"""Opt-in headless production Conversation, quiet start and terminal channel regressions."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
from unittest import mock

import _env
import common
import features
import piperd
import sessions


@unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                     "set PIPER_BROWSER_TESTS=1 for the isolated browser regression")
class AuditConvoQueueSSHTest(unittest.TestCase):
    def test_history_pages_queue_identity_and_explicit_terminal_reconnect(self):
        manager = _env.manager(self)
        root = _env.make_project("convo-queue-ssh", {"README.md": "Synthetic audit fixture\n"})
        key = "acme/shop"
        _env.register(key, root)
        session = sessions.Session(project=key, path=root, state="exited", title="Conversation pages",
                                   claude_mode="session", claude_session="audit-history")
        manager.sessions[session.id] = session
        rel = "projects/-audit/audit-history.jsonl"
        Path(session.claude_src, "projects/-audit").mkdir(parents=True, exist_ok=True)
        common.safe_write(session.claude_src, rel, ("\n".join(json.dumps({"type": "user", "message": {
            "content": "Message %04d: synthetic history content" % i}}) for i in range(4000)) + "\n").encode())
        features.save(onboarded=True)
        starts = [dict(id="queue-one", title=key, project=key, task="Fix checkout rounding", phase="queued",
                       agent="claude", runtime="hole", opts={}),
                  dict(id="queue-two", title=key, project=key, task="Add CSV export", phase="queued",
                       agent="codex", runtime="direct", opts={}, worktree={"name": "csv-export", "branch": "csv-export"})]
        manager.quiet.doc["starts"] = starts
        peers, delivered, checks = [], [], []
        failing = [False]

        class Peer:
            remote_id = "remote-synthetic-attachment"
            alive = True
            def __init__(self, eid, sid, callback):
                if failing[0]:
                    raise piperd.remotes.Unavailable("synthetic attach unavailable")
                self.callback = callback
                peers.append(self)
            def call(self, op, **kwargs):
                if op == "subscribe":
                    self.callback(dict(ev="data", data=base64.b64encode(b"Synthetic remote terminal\r\n").decode()))
                    return dict(ok=True, info=dict(cols=120, rows=34))
                if op == "input":
                    if not self.alive:
                        return dict(ok=False, error="exited")
                    value = base64.b64decode(kwargs["data"]).decode()
                    delivered.append(value)
                    self.callback(dict(ev="data", data=base64.b64encode(value.encode()).decode()))
                return dict(ok=True)
            def close(self):
                pass

        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        browser = "piper-convo-queue-ssh-%s" % os.getpid()

        def cli(*args):
            result = subprocess.run(["agent-browser", "--session", browser, "--json", *args],
                                    capture_output=True, text=True, timeout=35)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token("gui"), "[token]"))
            data = json.loads(result.stdout)
            self.assertTrue(data["success"], "agent-browser action failed")
            return data.get("data", {}).get("result")

        def ready(code):
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if cli("eval", code) is True:
                    return
                time.sleep(.1)
            self.fail(code)

        def shot(name):
            path = Path(os.environ["TEST_SANDBOX_WORK"], "convo-queue-ssh-" + name + ".png")
            cli("screenshot", str(path))
            if os.environ.get("PIPER_E2E_EXPORT_PNG") == "1":
                print("SCREENSHOT " + json.dumps({"name": path.name, "data": base64.b64encode(path.read_bytes()).decode()}), flush=True)

        def evaluate(script):
            return cli("eval", "(async()=>{" + script + "})()")

        with mock.patch.object(piperd, "MANAGER", manager), mock.patch.object(piperd, "PORT", server.server_port), \
                mock.patch.object(piperd.remotes, "get", return_value={"id": "ssh-fixture", "mode": "ssh"}), \
                mock.patch.object(piperd.remotes, "terminal", side_effect=Peer), \
                mock.patch.object(piperd.remotes, "proxy", side_effect=piperd.remotes.Unavailable("Synthetic control plane not exercised")):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                cli("open", piperd.gui_url())
                cli("set", "viewport", "1440", "1000")
                ready('!!document.querySelector(".beside")')
                evaluate("""window.auditErrors=[];addEventListener('error',e=>auditErrors.push(e.message));
                  addEventListener('unhandledrejection',e=>auditErrors.push(String(e.reason)));
                  const host=document.createElement('div');host.id='audit-host';
                  host.style.cssText='position:fixed;inset:0;z-index:10000;display:flex;flex-direction:column;background:var(--well)';
                  document.getElementById('app').style.display='none';document.body.append(host);window.auditHost=host;
                  const {createConvo}=await import('/piper/dist/convo.js');
                  window.auditConvo=createConvo({toast:m=>auditErrors.push(m),modal(){},closeModal(){},jump(){}});
                  auditConvo.el.style.cssText="flex:1;min-height:0";host.append(auditConvo.el);auditConvo.show(%s);return true;""" % json.dumps(session.id))
                ready('document.querySelectorAll("#audit-host .turn").length===400')
                self.assertIn("Showing latest 400 of 4,000", cli("eval", 'document.querySelector(".convo-history").textContent'))
                self.assertNotIn("Message 0000:", cli("eval", 'auditHost.textContent'))
                cli("eval", "document.querySelector('#audit-host .turns').scrollTop=0;true")
                shot("history-latest")
                cli("eval", "window.anchorBefore=document.querySelector('[data-turn=\"3600\"]').getBoundingClientRect().top;true")
                evaluate("const {api}=await import('/piper/dist/api.js');const original=api.conversation;api.conversation=async(...args)=>{api.conversation=original;await new Promise(resolve=>window.releaseOlder=resolve);throw Error('synthetic older-page transport error')};return true;")
                cli("click", "#audit-host .convo-history button")
                ready('document.querySelector(".convo-history button").textContent==="Loading…"&&document.querySelector(".convo-history button").disabled')
                cli("eval", "releaseOlder();true")
                ready('document.querySelector(".convo-history button").textContent==="Retry"')
                self.assertEqual(cli("eval", 'document.querySelectorAll("#audit-host .turn").length'), 400)
                shot("history-retry")
                cli("focus", "#audit-host .convo-history button")
                cli("press", "Enter")
                ready('document.querySelectorAll("#audit-host .turn").length===800')
                print("ANCHOR_DEBUG " + json.dumps(cli("eval", "({anchorBefore,after:document.querySelector('[data-turn=\"3600\"]').getBoundingClientRect().top,scroll:document.querySelector('.turns').scrollTop,height:document.querySelector('.turns').clientHeight,scrollHeight:document.querySelector('.turns').scrollHeight,convoHeight:document.querySelector('.convo').clientHeight})")), flush=True)
                self.assertLess(abs(cli("eval", "document.querySelector('[data-turn=\"3600\"]').getBoundingClientRect().top-anchorBefore")), 3)
                evaluate("const {api}=await import('/piper/dist/api.js');const original=api.conversation;window.refreshDone=false;api.conversation=async(...args)=>{try{return await original(...args)}finally{window.refreshDone=true;api.conversation=original}};auditConvo.show(%s);return true;" % json.dumps(session.id))
                ready('refreshDone===true')
                self.assertEqual(cli("eval", 'document.querySelectorAll("#audit-host .turn").length'), 800)
                checks.append("A16-F07 initial400/4000, explicit older page and stable scroll anchor")
                cli("fill", '#audit-host input[type="search"]', "Message 0000:")
                self.assertEqual(cli("eval", 'document.querySelectorAll("#audit-host .turn").length'), 0)
                self.assertIn("loaded messages only", cli("eval", 'document.querySelector(".convo-history").textContent'))
                for count in range(1200, 4001, 400):
                    cli("click", "#audit-host .convo-history button")
                    ready('document.querySelector(".convo-history").textContent.includes(%s)' % json.dumps("Showing all 4,000" if count == 4000 else "Showing latest " + format(count, ",") + " of 4,000"))
                self.assertEqual(cli("eval", 'document.querySelectorAll("#audit-host .turn").length'), 1)
                self.assertIn("Message 0000:", cli("eval", 'auditHost.textContent'))
                self.assertEqual(cli("eval", 'document.querySelector("#audit-host input").value'), "Message 0000:")
                self.assertIsNone(cli("eval", 'document.querySelector(".convo-history button")'))
                shot("history-all-filter")
                checks.append("A16-F07 all4000 reached manually; loaded-only filter and filter draft preserved")
                cli("set", "viewport", "1280", "633")
                evaluate("""auditConvo.hide();auditHost.replaceChildren();
                  const {createQuiet}=await import('/piper/dist/quiet.js');const {call}=await import('/piper/dist/api.js');
                  window.quietData=%s;window.auditQuiet=createQuiet({state:()=>quietData,sessions:()=>[],seen:()=>true,
                    markSeen(){},close(){},open(){},confirm(){},error:m=>auditErrors.push(m),changed(){auditQuiet.update()},
                    act:async body=>{quietData=(await call('POST','/api/quiet',body)).data;return quietData}});
                  auditHost.append(auditQuiet.el);auditQuiet.open('');return true;""" % json.dumps(manager.quiet.snapshot()))
                ready('document.querySelectorAll(".quiet-start-task").length===2')
                self.assertIn("Claude · Hole · main", cli("eval", 'auditHost.textContent'))
                self.assertIn("Codex · Direct · new worktree: csv-export", cli("eval", 'auditHost.textContent'))
                self.assertEqual(cli("eval", 'document.querySelectorAll(".quiet-start-task")[1].title'), "Add CSV export")
                cli("eval", "document.querySelectorAll('#audit-host details').forEach(d=>d.open=true);true")
                shot("queue-distinct")
                cli("click", '[aria-label="Remove queued start: Add CSV export"]')
                ready('document.querySelectorAll(".quiet-start-task").length===1')
                self.assertEqual([r["id"] for r in manager.quiet.doc["starts"]], ["queue-one"])
                checks.append("A12-001 distinct full tasks/runtime/worktree before actual HTTP Remove of selected row")
                evaluate("""auditHost.replaceChildren();const {TermView}=await import('/piper/dist/term.js');
                  const {hosts}=await import('/piper/dist/environment.js');
                  hosts.set('ssh-fixture',{host:{id:'ssh-fixture',label:'Synthetic SSH',mode:'ssh'},online:true,attemptedAt:Date.now()+60000,checkedAt:Date.now(),etag:'',error:''});
                  window.auditTerm=new TermView('piper-ref:ssh-fixture:session:remote-one','/synthetic',{
                    onRefused:m=>auditErrors.push(m),onExit:()=>auditErrors.push('remote process falsely exited'),onConnection(){}});
                  auditTerm.mount(auditHost);auditTerm.setInputState(true,true,'Synthetic remote',true);return true;""")
                ready('!!auditTerm.ws&&auditTerm.ws.readyState===1')
                cli("eval", "auditTerm.term.input('BEFORE-DROP',true);true")
                deadline = time.monotonic() + 5
                while "BEFORE-DROP" not in delivered and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertIn("BEFORE-DROP", delivered)
                peers[-1].alive = False
                peers[-1].callback(dict(ev="exit", rc=255))
                ready('document.querySelector(".terminal-channel").classList.contains("hidden")===false')
                self.assertIn("Remote session may still be running", cli("eval", 'document.querySelector(".terminal-channel").textContent'))
                cli("eval", "auditTerm.term.input('NEVER-REPLAY',true);true")
                self.assertNotIn("NEVER-REPLAY", delivered)
                shot("channel-closed")
                cli("click", ".terminal-channel button")
                ready('document.querySelector(".terminal-channel").classList.contains("hidden")===true')
                self.assertEqual(len(peers), 2)
                self.assertNotIn("NEVER-REPLAY", delivered)
                cli("eval", "auditTerm.term.input('AFTER-RECONNECT',true);true")
                deadline = time.monotonic() + 5
                while "AFTER-RECONNECT" not in delivered and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertIn("AFTER-RECONNECT", delivered)
                checks.append("A15-01 production WebSocket attachment exit, persistent banner, explicit attach and no input replay")
                peers[-1].callback(dict(ev="exit", rc=255)); failing[0] = True
                ready('document.querySelector(".terminal-channel").classList.contains("hidden")===false')
                cli("click", ".terminal-channel button")
                ready('document.querySelector(".terminal-channel").textContent.includes("Reconnect failed")')
                shot("reconnect-failed")
                cli("set", "viewport", "390", "844")
                self.assertLessEqual(cli("eval", 'document.querySelector(".terminal-channel").getBoundingClientRect().right'), 390)
                shot("channel-mobile")
                self.assertEqual(cli("eval", "auditErrors"), [])
                checks.append("A15-01 failed reconnect remains actionable and mobile banner stays inside viewport; noJSerrors")
                print("AUDIT_CONVO_QUEUE_SSH " + json.dumps({"checks": checks, "delivered": delivered}), flush=True)
            except Exception:
                print("GUI_DEBUG " + json.dumps({"starts": manager.quiet.doc["starts"], "browser": cli("eval", "({errors:auditErrors,text:auditHost?.textContent?.slice(0,3000),quietData:window.quietData})")}), flush=True)
                raise
            finally:
                try:
                    cli("eval", "window.auditTerm?.dispose();window.auditConvo?.hide();true")
                    cli("close")
                finally:
                    server.shutdown(); server.server_close()
