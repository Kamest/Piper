"""Real Manager/API → terminal reference → registered sibling Files regressions."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest
from unittest import mock

import _env
import common
import piperd
import projectfs
import projects
import sessions


def git_fixture(root):
    for args in (("init", "-qb", "main"), ("add", "."), ("commit", "-qm", "fixture baseline", "--allow-empty")):
        subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)


@unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                     "set PIPER_BROWSER_TESTS=1 for headless cross-project reference routing")
class CrossProjectReferenceBrowserTest(unittest.TestCase):
    def setUp(self):
        self.manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, self.manager)
        # the project registry is one file per test process: start from an empty one and put it back afterwards, so
        # another module's acme/app (whose checkout has a helper-kit/ folder) cannot make the sibling path ambiguous
        self.addCleanup(common.write_json, common.PROJECTS_FILE, common.read_json(common.PROJECTS_FILE, {"projects": {}}))
        common.write_json(common.PROJECTS_FILE, {"projects": {}})
        self.root = os.path.realpath(_env.make_project("reference-source", {"README.md": "Source checkout\n"}))
        git_fixture(self.root)
        _env.register("acme/app", self.root)
        self.sibling = os.path.realpath(_env.make_project("helper-kit", {".gitlab-ci.yml": "stages: [sibling-test]\n"}))
        git_fixture(self.sibling)
        _env.register("acme/helper-kit", self.sibling)
        self.session = sessions.Session(project="acme/app", path=self.root, title="Cross-project reference", state="waiting")
        self.session.save()
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, "MANAGER", self.manager))
        self.enterContext(mock.patch.object(piperd, "PORT", server.server_port))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.browser = "piper-cross-project-%s" % os.getpid()
        self.addCleanup(self.cli, "close")
        self.cli("open", piperd.gui_url())
        self.cli("set", "viewport", "1280", "800")
        self.ready('!!document.querySelector("#actbar")')
        sid = self.session.id
        child = self.manager.pty.call("spawn", id=sid, argv=[sys.executable, "-c", "import time;time.sleep(120)"],
                                      cwd=self.root, env=dict(os.environ), cols=80, rows=24)
        self.session.pid = child["pid"]
        self.cli("eval", '''(async()=>{
            const {TermView}=await import('/piper/dist/term.js');
            const connect=TermView.prototype.connect;
            TermView.prototype.connect=function(){
                if(this.id===%s){window.referenceView=this;return;}return connect.call(this);
            };return true;
        })()''' % json.dumps(sid))
        self.manager.sessions[sid] = self.session
        self.manager.bump()
        self.ready('!!document.querySelector(%s)' % json.dumps('#queue [data-sid="%s"]' % sid))
        self.select_terminal()

    def cli(self, *args):
        result = subprocess.run(["agent-browser", "--session", self.browser, "--json", *args],
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token("gui"), "[token]"))
        data = json.loads(result.stdout)
        self.assertTrue(data["success"], "agent-browser action failed")
        return data.get("data", {}).get("result")

    def ready(self, expression):
        self.cli("wait", "--fn", expression)

    def shot(self, name):
        target = Path(os.environ["TEST_SANDBOX_WORK"], name + ".png")
        self.cli("screenshot", str(target))
        print("CROSS_PROJECT_PNG " + name + " " + base64.b64encode(target.read_bytes()).decode(), flush=True)

    def select_terminal(self):
        self.cli("eval", 'document.querySelector(%s).click();true' % json.dumps('#queue [data-sid="%s"]' % self.session.id))
        self.cli("eval", '[...document.querySelectorAll("header .tab")].find(b=>b.textContent==="Terminal")?.click();true')
        self.ready('!!window.referenceView?.term.element')
        self.ready('referenceView.term.element.checkVisibility()')

    def open_path(self, path, *, pointer=True):
        self.cli("eval", '''(async()=>{
            const t=referenceView.term;referenceView.freeze(true);t.resize(100,12);t.reset();
            await new Promise(done=>t.write(%s,done));return true;
        })()''' % json.dumps(path + "\r\n"))
        self.assertTrue(self.cli("eval", 'referenceView.fileLinks(1).some(link=>link.text===%s)' % json.dumps(path)))
        if not pointer:
            self.cli("eval", 'referenceView.fileLinks(1).find(link=>link.text===%s).activate({});true' % json.dumps(path))
            return
        x, y = self.cli("eval", '''(()=>{const r=referenceView.host.querySelector('.xterm-screen').getBoundingClientRect();
            return [r.x+r.width/referenceView.term.cols*4.5,r.y+r.height/referenceView.term.rows*.5]})()''')
        self.cli("mouse", "move", "0", "0")
        self.cli("mouse", "move", str(x), str(y))
        self.ready('!!referenceView.host.querySelector(".xterm-cursor-pointer") || referenceView.term.element.classList.contains("xterm-cursor-pointer")')
        self.cli("mouse", "down")
        self.cli("mouse", "up")

    def test_sibling_file_exact_current_priority_and_ambiguous_choice(self):
        self.open_path("helper-kit/.gitlab-ci.yml")
        self.ready('document.querySelector(".files .cm-editor")?.textContent.includes("stages: [sibling-test]")')
        self.assertEqual(self.cli("eval", 'document.querySelector(".files .crumbs strong").textContent'), ".gitlab-ci.yml")
        self.shot("cross-project-sibling-file")

        # The very same relative token names a real file inside the source checkout now.
        current = Path(self.root, "helper-kit", ".gitlab-ci.yml")
        current.parent.mkdir()
        current.write_text("stages: [current-priority]\n")
        self.select_terminal()
        self.open_path("helper-kit/.gitlab-ci.yml", pointer=False)
        self.ready('document.querySelector(".files .cm-editor")?.textContent.includes("stages: [current-priority]")')
        self.shot("cross-project-current-priority")

        current.unlink()
        current.parent.rmdir()
        self.cli("eval", '''(async()=>{const {api}=await import('/piper/dist/api.js');
            const state=api.state;api.state=async(...args)=>{const result=await state(...args);
                window.referenceProjects=result.data?.projects;return result;};return true;})()''')
        other = os.path.realpath(_env.make_project("other/helper-kit", {".gitlab-ci.yml": "stages: [chosen-other]\n"}))
        git_fixture(other)
        # the checkout outlives the registry entry otherwise: once the discovery cache (60 s) expires, a later module's
        # projects list would carry team-x/other/helper-kit and make that module's helper-kit/… reference ambiguous
        self.addCleanup(projects._DISCOVERED.update, at=0.0)
        self.addCleanup(shutil.rmtree, os.path.dirname(other), True)
        _env.register("other/helper-kit", other)
        self.manager.bump()
        self.select_terminal()
        # Wait for the browser's actual state refresh to include the second registered project.
        self.ready('window.referenceProjects?.some(p=>p.key==="other/helper-kit")')
        self.open_path("helper-kit/.gitlab-ci.yml", pointer=False)
        self.ready('document.querySelector(".cmenu")?.textContent.includes("acme/helper-kit") && document.querySelector(".cmenu")?.textContent.includes("other/helper-kit")')
        self.assertFalse(self.cli("eval", 'document.querySelector(".files .cm-editor")?.textContent.includes("stages: [chosen-other]") ?? false'))
        self.shot("cross-project-ambiguous-choice")
        self.cli("eval", '[...document.querySelectorAll(".cmenu button")].find(b=>b.textContent.includes("other/helper-kit")).click();true')
        self.ready('document.querySelector(".files .cm-editor")?.textContent.includes("stages: [chosen-other]")')
        self.shot("cross-project-selected-file")

    def click_cell(self, col, row):
        x, y = self.cli("eval", '''(()=>{const r=referenceView.host.querySelector('.xterm-screen').getBoundingClientRect();
            return [r.x+r.width/referenceView.term.cols*%s,r.y+r.height/referenceView.term.rows*%s]})()''' % (col + .5, row + .5))
        self.cli("mouse", "move", "0", "0")
        self.cli("mouse", "move", str(x), str(y))
        self.ready('!!referenceView.host.querySelector(".xterm-cursor-pointer") || referenceView.term.element.classList.contains("xterm-cursor-pointer")')
        self.cli("mouse", "down")
        self.cli("mouse", "up")

    def write_rows(self, rows, cols=36):
        self.cli("eval", '''(async()=>{
            const t=referenceView.term;referenceView.freeze(true);t.resize(%d,12);t.reset();
            await new Promise(done=>t.write(%s,done));return true;
        })()''' % (cols, json.dumps("\r\n".join(rows) + "\r\n")))

    def phone_shell(self):
        self.enterContext(mock.patch.object(piperd, "PUBLIC", (*piperd.PUBLIC, "mobile.css")))
        self.cli("set", "viewport", "390", "844")
        self.cli("eval", '''(async()=>{
            const link=document.createElement('link'); link.rel='stylesheet'; link.href='/piper/mobile.css';
            await new Promise((done,fail)=>{link.onload=done;link.onerror=fail;document.head.append(link);});return true;
        })()''')
        self.select_terminal()
        self.ready('document.body.classList.contains("phone-shell")')

    def tap_first_row_link(self):
        self.assertTrue(self.cli("eval", '''(()=>{
            const r=referenceView.host.querySelector('.xterm-screen').getBoundingClientRect();
            return referenceView.tapLink(r.x+r.width/referenceView.term.cols*9.5,r.y+r.height/referenceView.term.rows*.5);
        })()'''))

    def test_full_row_path_before_a_sentence_opens_itself(self):
        # Reproduce a full path row with a sentence break that used to open a path ending in ".md.Do".
        Path(self.root, "development").mkdir(exist_ok=True)
        self.cli("set", "viewport", "1920", "900")   # the sandbox's absolute path makes a ~120-column row
        for extra, name in ((0, "1234-terminal-note.md"), (1, "1234-terminal-b.md"), (2, "1234-terminal-c.md")):
            target = Path(self.root, "development", name)
            target.write_text("# Draft %s\n" % name)
            row = "  " + str(target) + "."
            rows = ["● I prepared a draft review note for app:", row, "  Do not share the draft yet."]
            self.select_terminal()
            self.cli("eval", 'document.querySelectorAll(".toast").forEach(t=>t.remove());true')
            self.write_rows(rows, cols=len(row) + extra)   # the path ends 0, 1 or 2 cells before the edge
            self.assertEqual(self.cli("eval", 'referenceView.fileLinks(2).map(l=>l.text)'), [str(target)])
            self.assertEqual(self.cli("eval", 'referenceView.fileLinks(3).map(l=>l.text)'), [])
            if not extra:
                self.click_cell(6, 1)   # a real pointer click while the terminal has the whole width
            else:
                # once Files sits beside the terminal the wide fixture row overflows the narrower pane
                self.cli("eval", 'referenceView.fileLinks(2)[0].activate({});true')
            # a Markdown file opens in its preview: the crumb names the opened file
            self.ready('document.querySelector(".files .crumbs strong")?.textContent===%s' % json.dumps(name))
            self.assertFalse(self.cli("eval", '[...document.querySelectorAll(".toast")].some(t=>t.textContent.includes("not found"))'))
        self.shot("full-row-path-before-sentence")

    def test_generated_wrap_matrix_in_xterm(self):
        # The unit matrix (terminal-wrap.test.mjs) emulates the row breaks; this replays a deterministic sample of the
        # same wrap-ansi layouts through the real xterm buffer and TermView.fileLinks.
        script = Path(__file__).with_name("termwrap-cases.mjs")
        out = subprocess.run(["node", str(script), "20", "24", "41", "63", "81", "117"], capture_output=True, text=True,
                             check=True, timeout=120).stdout
        every = json.loads(out)
        sample = [c for c in every[::37] if not c["ambiguous"]]
        self.assertGreater(len(sample), 7000)
        self.select_terminal()
        failures = []
        for i in range(0, len(sample), 300):
            batch = [{k: c[k] for k in ("cols", "rows", "pathRows", "name", "shown")} for c in sample[i:i + 300]]
            failures += self.cli("eval", '''(async()=>{
                const t=referenceView.term;referenceView.freeze(true);const bad=[];
                for(const c of %s){
                    t.resize(c.cols,Math.max(c.rows.length+2,6));t.reset();
                    await new Promise(done=>t.write(c.rows.join("\\r\\n")+"\\r\\n",done));
                    for(const row of c.pathRows){
                        const y=row+1,here=referenceView.fileLinks(y).filter(l=>l.range.start.y<=y&&l.range.end.y>=y)
                            .map(l=>({text:l.text.replace(/^`(.*)`$/,"$1")}));   // a code quote may frame the link
                        const own=here.some(l=>l.text===c.shown||l.text.endsWith(c.shown)&&!/\\s/.test(l.text));
                        const glued=here.filter(l=>l.text!==c.shown&&l.text.includes(c.shown)&&!l.text.endsWith(c.shown));
                        const cut=here.filter(l=>l.text!==c.shown&&c.shown.includes(l.text));
                        if(!own||glued.length||cut.length){bad.push(c.name+" row "+row+": "+JSON.stringify(here.map(l=>l.text))+"\\n  "+c.rows.join("\\n  "));break;}
                    }
                }
                return bad;
            })()''' % json.dumps(batch))
        self.assertEqual(failures, [], "%d/%d layouts:\n%s" % (len(failures), len(sample), "\n".join(failures[:20])))

    def test_ambiguous_url_only_opens_the_selected_destination(self):
        url = "https://example.com/reports/42"
        self.cli("eval", '''(async()=>{
            const {TERM_FLAGS} = await import('/piper/dist/term.js'); TERM_FLAGS.hostLinks=false;
            window.referenceOpened=[]; window.open=url=>{referenceOpened.push(url);return null;};return true;
        })()''')
        self.write_rows([url, "draft follows"], cols=len(url))
        self.click_cell(10, 0)
        menu = 'document.querySelector(\'.cmenu[aria-label="Choose link"]\')'
        self.ready(f'!!{menu}')
        self.assertEqual(self.cli("eval", f'[...{menu}.querySelectorAll("button")].map(b=>b.dataset.menuId)'),
                         [url, url + "draft"])
        self.shot("ambiguous-url-choice")
        self.cli("press", "Escape")
        self.ready(f'!{menu}')
        self.assertEqual(self.cli("eval", 'referenceOpened'), [])
        for i, destination in enumerate((url, url + "draft")):
            self.click_cell(11 + i, 0)
            self.ready(f'!!{menu}')
            self.cli("eval", f'{menu}.querySelectorAll("button")[{i}].click();true')
            self.ready(f'!{menu} && referenceOpened.at(-1)==={json.dumps(destination)}')
        self.assertEqual(self.cli("eval", 'referenceOpened'), [url, url + "draft"])

        # OSC 8 declares the target directly even when its caption has the same ambiguous visible rows.
        self.write_rows(["Explicit link", "\x1b]8;;https://example.com/explicit\x1b\\" + url + "\x1b]8;;\x1b\\", "draft follows"], cols=len(url))
        self.click_cell(13, 1)
        self.ready(f'!{menu} && referenceOpened.at(-1)==="https://example.com/explicit"')

        self.phone_shell()
        self.write_rows([url, "draft follows"], cols=len(url))
        self.tap_first_row_link()
        sheet = 'document.querySelector(\'.sheet[aria-label="Open which link?"]\')'
        self.ready(f'!!{sheet}')
        self.assertEqual(self.cli("eval", f'[...{sheet}.querySelectorAll(".sheet-label")].map(e=>e.textContent)'), ["42", "42draft"])
        self.cli("eval", f'{sheet}.querySelectorAll(".sheet-row")[1].click();true')
        self.ready(f'!{sheet} && referenceOpened.at(-1)==={json.dumps(url + "draft")}')

    def test_phone_ambiguous_file_choices_open_the_selected_line(self):
        relative = "development/sample/review.ts"
        target = Path(self.root, relative)
        target.parent.mkdir(parents=True)
        target.write_text("\n".join(f"export const row{i} = {i};" for i in range(1, 130)))
        self.phone_shell()
        self.cli("eval", '''(async()=>{
            const {EditorView}=await import('cm');
            window.referenceLine=()=>{
                const editor=document.querySelector('.files .cm-editor');
                const view=editor && EditorView.findFromDOM(editor);
                return view?.state.doc.lineAt(view.state.selection.main.head).number;
            };return true;
        })()''')
        head = f"{relative}:12"
        sheet = 'document.querySelector(\'.sheet[aria-label="Open which target?"]\')'
        for line in (12, 123):
            if line == 123:
                self.cli("eval", 'document.querySelector("header.ph-pushed .ph-back").click();true')
            self.ready('referenceView.term.element.checkVisibility()')
            self.write_rows([head, "3 items remain"], cols=len(head))
            self.tap_first_row_link()
            self.ready(f'!!{sheet}')
            self.assertEqual(self.cli("eval", f'[...{sheet}.querySelectorAll(".sheet-label")].map(e=>e.textContent)'), ["Line 12", "Line 123"])
            if line == 12:
                self.shot("ambiguous-file-choice-phone")
            self.cli("eval", f'[...{sheet}.querySelectorAll("button")].find(b=>b.textContent===\'Line {line}\').click();true')
            self.ready(f'!{sheet} && referenceLine()==={line}')

    def test_path_split_inside_its_first_segment_opens_from_either_row(self):
        # reported: a narrow pane where Claude Code broke "development/…" as "dev" | "elopment/…" under a list indent
        folder = Path(self.root, "development", "all-projects-session")
        folder.mkdir(parents=True)
        (folder / "before-picker-notes.txt").write_text("split segment\n")
        rows = ["- The screenshots are saved in dev", "  elopment/all-projects-session/:", "  before-picker.png,"]
        for col, row in ((33, 0), (10, 1)):
            self.select_terminal()
            self.write_rows(rows)
            self.click_cell(col, row)
            self.ready('document.querySelector(".files")?.textContent.includes("before-picker-notes.txt")')
            self.assertFalse(self.cli("eval", '[...document.querySelectorAll(".toast")].some(t=>t.textContent.includes("File not found"))'))
        self.shot("split-segment-directory")

        # A short prose word that merely ends at the edge fit its row, so wrap-ansi never cut it: the path stands alone.
        Path(self.root, "src").mkdir()
        Path(self.root, "src", "second.ts").write_text("export const fallback = 1;\n")
        self.select_terminal()
        self.write_rows(["- Please open the following file in", "  src/second.ts"])
        self.assertEqual(self.cli("eval", 'referenceView.fileLinks(2).map(l=>l.text)'), ["src/second.ts"])
        self.cli("eval", 'referenceView.fileLinks(2)[0].activate({});true')
        self.ready('document.querySelector(".files .cm-editor")?.textContent.includes("fallback = 1")')
        self.shot("split-segment-prose-fallback")

    def test_bare_screenshot_name_under_a_development_line_opens(self):
        # reported: "Screenshoty jsou v development/:" then a bare "- 156-test-dic-abc.png"; a nested worktree
        # filled the 20000-file listing the link used, so development/ was never reached ("File not found")
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        Path(self.root, "development").mkdir(exist_ok=True)
        Path(self.root, "development", "156-test-dic-abc.png").write_bytes(png)
        nested = Path(self.root, ".claude", "worktrees", "130")
        (nested / "development").mkdir(parents=True)
        (nested / ".git").write_text("gitdir: elsewhere\n")
        (nested / "development" / "156-test-dic-abc.png").write_bytes(png)
        with mock.patch.object(projectfs, "MAX_FILES", 2):   # the listing cap no longer decides
            self.select_terminal()
            self.write_rows(["Screenshoty jsou v development/:", "- 156-test-dic-abc.png"], cols=60)
            self.assertEqual(self.cli("eval", 'referenceView.fileLinks(2).map(l=>l.text)'), ["156-test-dic-abc.png"])
            self.cli("eval", 'referenceView.fileLinks(2)[0].activate({});true')
            self.ready('document.querySelector(".imageview-info")?.title==="development/156-test-dic-abc.png"')
        self.assertFalse(self.cli("eval", '[...document.querySelectorAll(".toast")].some(t=>t.textContent.includes("not found"))'))
        self.shot("bare-screenshot-name")
