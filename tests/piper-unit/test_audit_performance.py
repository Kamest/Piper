"""Opt-in real HTTP/browser budgets on a 35k-file repository and 64 distinct snapshots."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
import urllib.parse
from unittest import mock

import _env
import common
import features
import piperd
import test_workbench97 as wb


@unittest.skipUnless(os.environ.get("PIPER_PERFORMANCE_TESTS") == "1" and shutil.which("agent-browser"),
                     "set PIPER_PERFORMANCE_TESTS=1 for the isolated 35k HTTP/browser budget")
class AuditPerformanceTest(unittest.TestCase):
    def test_large_repository_search_and_review_request_budgets(self):
        wb.Workbench97Test.setUpClass()
        self.addCleanup(wb.Workbench97Test.tearDownClass)
        helper = wb.Workbench97Test("test_review_preparation_git_calls_do_not_grow_per_plain_file")
        self.addCleanup(helper.doCleanups)
        manager = helper.m
        root = Path(_env.make_project("request-budget", {}))
        paths = []
        for n in range(35000):
            path = root / ("packages/pkg-%03d/component-%d/file-%03d.%s" %
                           (n // 500, n // 100 % 5, n % 100, ("ts", "py", "md", "json", "css")[n % 5]))
            path.parent.mkdir(parents=True, exist_ok=True)
            text = "searchable_marker_%05d\n" % n + "synthetic safe content\n" * (1 + n % 80)
            if n in (499, 999):
                text += "large synthetic content\n" * 50000
            path.write_text(text)
            if n % 500 == 0 and len(paths) < 64:
                paths.append(path.relative_to(root).as_posix())
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "test@example.com"),
                     ("config", "user.name", "Test"), ("add", "-A"), ("commit", "-qm", "Synthetic baseline"),
                     ("config", "grep.threads", "1")):
            wb.git(str(root), *args)
        key = "acme/request-budget"
        _env.register(key, str(root))
        session = helper._session(project=key, path=str(root), title="Request budget", at_prompt=True)
        previous = manager._snapshot(session, "budget.index")
        session.turns = []
        for n, rel in enumerate(paths, 1):
            with (root / rel).open("a") as stream:
                stream.write("durable change %d\n" % n)
            if n == 64:
                for earlier in paths[:-1]:
                    with (root / earlier).open("a") as stream:
                        stream.write("last turn 64\n")
            current = manager._snapshot(session, "budget.index")
            session.turns.append({"n": n, "prompt": "Change %d" % n, "started": n,
                                  "ended": n + 1, "from": previous, "to": current})
            previous = current
        features.save(onboarded=True)
        requests, measurements = [], []

        class MeasuredHandler(piperd.Handler):
            def do_GET(self):
                start = time.monotonic()
                try:
                    return super().do_GET()
                finally:
                    path = urllib.parse.urlsplit(self.path).path
                    if path == "/api/fs/grep" or path.endswith(("/review", "/review/check")):
                        requests.append({"path": path, "seconds": time.monotonic() - start})

        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), MeasuredHandler)
        browser = "piper-budget-" + session.id

        def cli(*args):
            result = subprocess.run(["agent-browser", "--session", browser, "--json", *args],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token("gui"), "[token]"))
            data = json.loads(result.stdout)
            self.assertTrue(data["success"], "agent-browser action failed")
            return data.get("data", {}).get("result")

        def ready(code):
            self.assertTrue(wb.wait(lambda: cli("eval", code) is True, timeout=30), code)

        def shot(name):
            target = Path(os.environ["TEST_SANDBOX_WORK"], "performance-" + name + ".png")
            cli("screenshot", str(target))
            if os.environ.get("PIPER_E2E_EXPORT_PNG") == "1":
                print("SCREENSHOT " + json.dumps({"name": target.name,
                      "data": base64.b64encode(target.read_bytes()).decode()}), flush=True)

        def measured(name, expression):
            result = cli("eval", "(async()=>{const m=await import('/piper/dist/api.js');const start=performance.now();"
                         "const r=await (" + expression + ");return {seconds:(performance.now()-start)/1000,data:r.data};})()")
            measurements.append({"name": name, "seconds": result["seconds"]})
            print("PERFORMANCE_MEASURE " + json.dumps(measurements[-1]), flush=True)
            self.assertLess(result["seconds"], 2.0, name)
            return result["data"]

        with mock.patch.object(piperd, "MANAGER", manager), mock.patch.object(piperd, "PORT", server.server_port), \
                mock.patch.object(manager, "sessions", {session.id: session}):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                cli("open", piperd.gui_url())
                cli("set", "viewport", "1440", "1000")
                ready('!!document.querySelector(\'button[aria-label="context panel"]\')')
                cli("eval", "window.budgetErrors=[];addEventListener('error',e=>budgetErrors.push(e.message));addEventListener('unhandledrejection',e=>budgetErrors.push(String(e.reason)));true")
                for index, query in enumerate(("searchable_marker_01234", "no_such_marker", "searchable_marker_01234")):
                    data = measured("search-%d" % index, "m.fsGrep(%s,%s,'')" % (json.dumps(key), json.dumps(query)))
                    self.assertEqual(len(data["matches"]), 0 if index == 1 else 1)
                cli("press", "Meta+Shift+f" if os.sys.platform == "darwin" else "Control+Shift+f")
                ready('!!document.querySelector(".sq")')
                cli("fill", ".sq", "searchable_marker_01234")
                cli("press", "Enter")
                ready('document.querySelector(".hit")?.textContent.includes("searchable_marker_01234")===true')
                shot("search")
                # The historical request uses its own actual snapshots, never reused stand-in trees.
                turn = measured("turn-64", "m.api.reviewFiles(%s,'turn',64)" % json.dumps(session.id))
                self.assertEqual({row["path"] for row in turn["files"]}, set(paths))
                head = measured("head", "m.api.reviewFiles(%s,'head')" % json.dumps(session.id))
                self.assertEqual(len(head["files"]), 64)
                check = measured("review-check", "m.api.reviewCheck(%s,%s)" % (json.dumps(session.id), json.dumps(head["id"])))
                self.assertEqual(check["changed"], [])
                cli("press", "Escape")
                cli("click", 'button.tab[title="the session terminal"]')
                cli("eval", "const b=document.querySelector('button[aria-label=\"context panel\"]');if(b.getAttribute('aria-pressed')!=='true')b.click();true")
                ready('!!document.querySelector(\'button[aria-label="Review the whole session"]\')')
                cli("click", 'button[aria-label="Review the whole session"]')
                ready('document.querySelector(\'[aria-label="Review scope"]\')?.disabled===false&&document.querySelectorAll(".review-merge .cm-editor").length===2')
                cli("select", '[aria-label="Review scope"]', "head")
                ready('document.querySelectorAll(".review-file-name").length===64')
                target = '[aria-label="Review ' + paths[-1] + '"]'
                cli("click", target)
                ready('document.querySelector(".review-merge .cm-merge-b")?.textContent.includes("durable change 64")===true')
                shot("review")
                with (root / paths[-1]).open("a") as stream:
                    stream.write("external live edit\n")
                ready('document.querySelector(".review-file.active")?.classList.contains("stale")===true')
                self.assertNotIn("external live edit", cli("eval", 'document.querySelector(".review-merge .cm-merge-b").textContent'))
                shot("review-stale")
                self.assertEqual(cli("eval", "budgetErrors"), [])
                self.assertTrue(any(x["path"].endswith("/review/check") for x in requests))
                for request in requests:
                    self.assertLess(request["seconds"], 2.0, request["path"])
                print("PERFORMANCE_RESULT " + json.dumps({"files": 35000, "distinct_turns": 64,
                      "changed_paths": 64, "measurements": measurements, "requests": requests,
                      "browser_errors": []}), flush=True)
            finally:
                subprocess.run(["agent-browser", "--session", browser, "close"], capture_output=True, timeout=30)
                server.shutdown()
                server.server_close()
