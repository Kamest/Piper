"""Headless real-client Git moves and terminal soft-wrap reference routing regressions."""
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
import gitops
import piperd
import sessions


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], check=True, capture_output=True).stdout.decode()


@unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                     "set PIPER_BROWSER_TESTS=1 for the headless Git/reference flows")
class GitReferenceBrowserTest(unittest.TestCase):
    def setUp(self):
        self.manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, self.manager)
        self.root = os.path.realpath(_env.make_project("git-reference-" + self._testMethodName, {"src/Original file.txt": "old line\n" * 30}))
        git(self.root, "init", "-qb", "main")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "fixture baseline")
        self.key = "acme/reference"
        _env.register(self.key, self.root, gitlab=["acme/app"])
        self.session = sessions.Session(project=self.key, path=self.root, title="Reference fixture", state="waiting",
                                        gitlab=["acme/app"], gitlab_extra=["acme/base"],
                                        issue={"gl": "acme/base", "iid": 12, "type": "issue", "title": "Base issue"})
        self.session.save()
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, "MANAGER", self.manager))
        self.enterContext(mock.patch.object(piperd, "PORT", server.server_port))
        self.enterContext(mock.patch.object(gitops, "FAIL_CLOSED", False))
        self.enterContext(mock.patch.object(self.manager, "gitlab_host", return_value="https://example.com"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.browser = "piper-git-reference-%s" % os.getpid()
        self.addCleanup(self.cli, "close")
        self.cli("open", piperd.gui_url())
        self.cli("set", "viewport", "1280", "800")
        self.ready('!!document.querySelector("#actbar")')

    def cli(self, *args):
        result = subprocess.run(["agent-browser", "--session", self.browser, "--json", *args],
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token("gui"), "[token]"))
        data = json.loads(result.stdout)
        self.assertTrue(data["success"], "agent-browser action failed")
        return data.get("data", {}).get("result")

    def ready(self, expression):
        try:
            self.cli("wait", "--fn", expression)
        except AssertionError:
            print("REFERENCE_DIAGNOSTIC", self.cli("eval", "document.body.innerText.slice(-2000)"), flush=True)
            raise

    def shot(self, name):
        target = Path(os.environ["TEST_SANDBOX_WORK"], name + ".png")
        self.cli("screenshot", str(target))
        print("FEEDBACK_PNG " + name + " " + base64.b64encode(target.read_bytes()).decode(), flush=True)

    def test_git_unstaged_edited_rename_review_stage_and_committed_window(self):
        old, new = "src/Original file.txt", "src/Moved résumé file.txt"
        os.rename(os.path.join(self.root, old), os.path.join(self.root, new))
        with open(os.path.join(self.root, new), "a") as f:
            f.write("edited after moving\n")
        self.manager.sessions[self.session.id] = self.session
        self.cli("eval", '''(async()=>{
            const {createGitPanel}=await import('/piper/dist/gitpanel.js');
            document.body.replaceChildren(); localStorage.setItem('piper.git.source','index');
            window.gitFixtureModal=(title,...nodes)=>{
                document.querySelector('#git-fixture-overlay')?.remove();
                const overlay=document.createElement('div');overlay.id='git-fixture-overlay';overlay.className='overlay open';
                const box=document.createElement('div');box.className='modal';
                const close=document.createElement('button');close.textContent='Close fixture dialog';
                close.onclick=()=>overlay.remove();box.append(...nodes.filter(Boolean),close);overlay.append(box);document.body.append(overlay);
            };
            const s=%s;
            window.gitFixture=createGitPanel({session:()=>s,projectPath:()=>s.path,project:()=>s.project,
                requireReview:()=>false,toast:text=>console.info(text),modal:gitFixtureModal,
                closeModal:()=>document.querySelector('#git-fixture-overlay')?.remove(),send:async()=>true,
                gitlab:()=>false,ciEnabled:()=>false});
            document.body.append(gitFixture.el);gitFixture.show(s.id);return true;
        })()''' % json.dumps({"id": self.session.id, "project": self.key, "path": self.root, "state": "waiting"}))
        self.ready('document.querySelectorAll(".index-file").length===1')
        self.assertEqual(self.cli("eval", 'document.querySelector(".index-file .gmark").textContent'), "R")
        self.assertIn("Original file.txt → Moved résumé file.txt", self.cli("eval", 'document.querySelector(".index-file").textContent'))
        self.shot("f03-unstaged-move")
        self.cli("click", '.index-file [data-git-path]')
        self.ready('document.querySelectorAll(".review-merge .cm-editor").length===2')
        self.ready('document.querySelector(".review-merge")?.textContent.includes("edited after moving")')
        self.shot("f03-move-review")
        self.cli("eval", '[...document.querySelectorAll("#git-fixture-overlay button")].find(b=>b.textContent==="Close fixture dialog").click();true')
        self.cli("eval", '[...document.querySelectorAll(".index-file button")].find(b=>b.textContent==="Stage").click();true')
        self.ready('document.querySelector(".index-file .gmark")?.getAttribute("aria-label")==="renamed, staged"')
        self.assertTrue(git(self.root, "status", "--porcelain").startswith('R '))
        self.shot("f03-staged-move")
        git(self.root, "commit", "-qm", "move and edit")
        self.cli("eval", '(async()=>{const {openGitWindow}=await import("/piper/dist/gitwindow.js");openGitWindow({toast:()=>{},modal:gitFixtureModal,closeModal:()=>document.querySelector("#git-fixture-overlay")?.remove()},%s);return true;})()' % json.dumps(self.key))
        self.ready('document.querySelector(".gw-files")?.textContent.includes("Original file.txt")')
        self.assertIn("Moved résumé file.txt", self.cli("eval", 'document.querySelector(".gw-files").textContent'))
        self.shot("f03-git-window-move")

    def test_cli_hard_wrapped_file_routes_to_registered_sibling_and_whole_url(self):
        sibling = os.path.realpath(_env.make_project("helper-kit", {".gitlab-ci.yml": "stages: [test]\n"}))
        git(sibling, "init", "-qb", "main")
        git(sibling, "add", ".")
        git(sibling, "commit", "-qm", "fixture baseline")
        _env.register("acme/helper-kit", sibling)
        sid = self.session.id
        self.manager.policy["open_links_on_host"] = True
        child = self.manager.pty.call("spawn", id=sid, argv=[sys.executable, "-c", "import time;time.sleep(120)"],
                                      cwd=self.root, env=dict(os.environ), cols=80, rows=24)
        self.session.pid = child["pid"]
        self.cli("eval", '''(async()=>{
            const {TermView}=await import('/piper/dist/term.js');
            const connect=TermView.prototype.connect;
            TermView.prototype.connect=function(){
                if(this.id===%s){window.referenceView=this;return;}return connect.call(this);
            };
            window.referenceRequests=[];
            const original=window.fetch.bind(window);
            window.fetch=async(input,init={})=>{
                const path=new URL(typeof input==='string'?input:input.url,location.href).pathname;
                if(path==='/api/open-url'){
                    referenceRequests.push(JSON.parse(init.body));
                    return new Response('{"opened":true}',{headers:{'Content-Type':'application/json'}});
                }
                return original(input,init);
            };return true;
        })()''' % json.dumps(sid))
        self.manager.sessions[sid] = self.session
        self.manager.bump()
        self.ready("!!document.querySelector(%s)" % json.dumps('#queue [data-sid="%s"]' % sid))
        self.cli("eval", "document.querySelector(%s).click();true" % json.dumps('#queue [data-sid="%s"]' % sid))
        self.ready('!!window.referenceView?.term.element')
        self.cli("eval", '(async()=>{window.referenceFlags=(await import("/piper/dist/term.js")).TERM_FLAGS;return true;})()')
        self.ready('referenceFlags.hostLinks===true')
        url = "https://example.com/long-reference?q=one&page=2"
        self.cli("eval", '''(async()=>{
            referenceView.freeze(true);const t=referenceView.term;t.resize(80,12);t.reset();
            await new Promise(done=>t.write(%s,done));return true;
        })()''' % json.dumps("Please open helper-\r\n  kit/.gitlab-ci.yml\r\n" +
                              "See https://example.com/long-\r\n  reference?q=one&\r\n  page=2\r\n"))
        self.assertFalse(self.cli("eval", 'referenceView.term.buffer.active.getLine(1).isWrapped'))
        self.assertTrue(self.cli("eval", 'referenceView.fileLinks(2).some(link=>link.text==="helper-kit/.gitlab-ci.yml")'))
        self.shot("f05-hard-wrapped-terminal")

        def click_cell(col, row):
            x, y = self.cli("eval", '(()=>{const r=referenceView.host.querySelector(".xterm-screen").getBoundingClientRect();return [r.x+r.width/referenceView.term.cols*%s,r.y+r.height/referenceView.term.rows*%s]})()' % (col - .5, row - .5))
            pointer = '!!referenceView.host.querySelector(".xterm-cursor-pointer") || referenceView.term.element.classList.contains("xterm-cursor-pointer")'
            self.cli("mouse", "move", "0", "0")
            # under load the previous link's pointer outlived the move away: wait until it is gone, so the pointer
            # below is this cell's link and the click is not sent before the linkifier found it
            self.ready("!(%s)" % pointer)
            self.cli("mouse", "move", str(x), str(y))
            self.ready(pointer)
            self.cli("mouse", "down")
            self.cli("mouse", "up")

        # Clicking the first URL row must use the complete target, not WebLinksAddon's valid prefix.
        click_cell(12, 3)
        self.ready('referenceRequests.some(request=>request.url===%s)' % json.dumps(url))
        self.assertEqual(self.cli("eval", "referenceRequests.map(request=>request.url)"), [url])
        click_cell(5, 4)
        self.ready('referenceRequests.length===2')
        self.assertEqual(self.cli("eval", "referenceRequests.map(request=>request.url)"), [url, url])
        # Real pointer activation on the path's continuation opens the sibling's hidden file via human Files.
        click_cell(5, 2)
        self.ready('document.querySelector(".files .crumbs strong")?.textContent===".gitlab-ci.yml"')
        self.ready('document.querySelector(".files .cm-editor")?.textContent.includes("stages: [test]")')
        self.shot("f05-hard-wrapped-sibling-file")

    def test_soft_wrapped_terminal_file_url_and_issue_use_actual_session_routes(self):
        sid = self.session.id
        self.manager.policy["open_links_on_host"] = True
        child = self.manager.pty.call("spawn", id=sid, argv=[sys.executable, "-c", "import time;time.sleep(120)"],
                                      cwd=self.root, env=dict(os.environ), cols=80, rows=24)
        self.session.pid = child["pid"]
        self.cli("eval", '''(async()=>{
            const {TermView}=await import('/piper/dist/term.js');
            const connect=TermView.prototype.connect;
            TermView.prototype.connect=function(){
                if(this.id===%s){window.referenceView=this;return;}return connect.call(this);
            };
            window.referenceRequests=[];
            const original=window.fetch.bind(window);
            window.fetch=async(input,init={})=>{
                const path=new URL(typeof input==='string'?input:input.url,location.href).pathname;
                if(path==='/api/open-url'){
                    referenceRequests.push(JSON.parse(init.body));
                    return new Response('{"opened":true}',{headers:{'Content-Type':'application/json'}});
                }
                return original(input,init);
            };return true;
        })()''' % json.dumps(sid))
        self.manager.sessions[sid] = self.session
        self.ready('!!document.querySelector("#queue [data-sid=\\"%s\\"]")' % sid)
        self.cli("eval", "document.querySelector(%s).click();true" % json.dumps('#queue [data-sid="%s"]' % sid))
        self.ready('!!window.referenceView?.term.element')
        self.cli("eval", '(async()=>{window.referenceFlags=(await import("/piper/dist/term.js")).TERM_FLAGS;return true;})()')
        self.ready('referenceFlags.hostLinks===true')
        file_url = Path(os.path.join(self.root, "src/Original file.txt")).as_uri() + "#L2"
        url = "https://example.com/acme/base/-/issues/12?description=" + "wrapped" * 12
        self.cli("eval", '''(async()=>{
            referenceView.freeze(true);const t=referenceView.term;t.resize(40,12);
            await new Promise(done=>t.write(%s,done));return true;
        })()''' % json.dumps(file_url + "\r\n" + url + "\r\n#12\r\nacme/other#12\r\n#13\r\n"))
        self.ready('referenceView.term.buffer.active.getLine(1)?.isWrapped===true')
        self.assertTrue(self.cli("eval", 'referenceView.fileLinks(2).some(link=>link.text===%s)' % json.dumps(file_url)))
        self.shot("f05-soft-wrapped-terminal")
        row = self.cli("eval", '(()=>{for(let row=1;row<=12;row++){const link=referenceView.fileLinks(row).find(link=>link.text===%s);if(link)return link.range.start.y+1;}throw Error("wrapped URL missing")})()' % json.dumps(url))
        x, y = self.cli("eval", '(()=>{const r=referenceView.host.querySelector(".xterm-screen").getBoundingClientRect();return [r.x+r.width/referenceView.term.cols*4.5,r.y+r.height/referenceView.term.rows*(%s-0.5)]})()' % row)
        self.cli("mouse", "move", "0", "0")
        self.ready('!(!!referenceView.host.querySelector(".xterm-cursor-pointer") || referenceView.term.element.classList.contains("xterm-cursor-pointer"))')
        self.cli("mouse", "move", str(x), str(y))
        self.ready('!!referenceView.host.querySelector(".xterm-cursor-pointer") || referenceView.term.element.classList.contains("xterm-cursor-pointer")')
        self.cli("mouse", "down")
        self.cli("mouse", "up")
        self.ready('referenceRequests.some(request=>request.url===%s)' % json.dumps(url))
        # A real mouse click on continuation row 2 exercises xterm's link hit testing too.
        x, y = self.cli("eval", '(()=>{const r=referenceView.host.querySelector(".xterm-screen").getBoundingClientRect();return [r.x+r.width/referenceView.term.cols*4.5,r.y+r.height/referenceView.term.rows*1.5]})()')
        self.cli("mouse", "move", "0", "0")
        self.ready('!(!!referenceView.host.querySelector(".xterm-cursor-pointer") || referenceView.term.element.classList.contains("xterm-cursor-pointer"))')
        self.cli("mouse", "move", str(x), str(y))
        self.ready('!!referenceView.host.querySelector(".xterm-cursor-pointer") || referenceView.term.element.classList.contains("xterm-cursor-pointer")')
        self.cli("mouse", "down")
        self.cli("mouse", "up")
        self.ready('document.querySelector(".files .crumbs strong")?.textContent==="Original file.txt"')
        self.shot("f05-wrapped-file-open")
        self.cli("eval", '(()=>{for(let row=1;row<=12;row++){for(const link of referenceView.fileLinks(row)){if(link.text==="#12"||link.text==="acme/other#12")link.activate({});}}return true;})()')
        self.ready('referenceRequests.some(request=>request.url==="https://example.com/acme/base/-/issues/12") && referenceRequests.some(request=>request.url==="https://example.com/acme/other/-/issues/12")')
        opened = self.cli("eval", "referenceRequests.map(request=>request.url)")
        self.assertNotIn("https://example.com/acme/app/-/issues/12", opened)
        self.shot("f05-issue-project-context")
        self.cli("eval", '(()=>{for(let row=1;row<=12;row++){const link=referenceView.fileLinks(row).find(link=>link.text==="#13");if(link){link.activate({});return true}}throw Error("ambiguous issue link missing")})()')
        self.ready('document.querySelector(".cmenu")?.textContent.includes("acme/app") && document.querySelector(".cmenu")?.textContent.includes("acme/base")')
        self.assertFalse(self.cli("eval", 'referenceRequests.some(request=>request.url.endsWith("/issues/13"))'),
                         "an ambiguous bare issue must wait for the human's project choice")
        self.shot("f05-ambiguous-project-choice")
        self.cli("eval", '[...document.querySelectorAll(".cmenu button")].find(b=>b.textContent.includes("acme/base")).click();true')
        self.ready('referenceRequests.some(request=>request.url==="https://example.com/acme/base/-/issues/13")')
        # The same references must route from Markdown in the actual Conversation view.
        # One native transcript record exercises Manager.conversation/digest over HTTP,
        # rather than replacing the conversation renderer or mocking its endpoint.
        native = "feedback-reference-transcript"
        transcript = Path(self.session.cfg, "projects", "-fixture", native + ".jsonl")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        text = '## Reference checks\n\nOpen "src/Original file.txt:9".\n\nLinked #12.\n\nQualified acme/other#12.\n\nAmbiguous #13.\n'
        transcript.write_text(json.dumps({"type": "assistant", "sessionId": native,
                                          "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}) + "\n")
        self.session.claude_session = native
        self.session.save()
        self.manager.bump()
        self.cli("eval", "referenceRequests=[];true")

        def conversation():
            self.cli("eval", '[...document.querySelectorAll("header .tab")].find(b=>b.textContent==="Conversation").click();true')
            self.ready('document.querySelector(".convo .tmd h2")?.textContent==="Reference checks"')

        def click_markdown_reference(label):
            selector = '.convo .tmd button[title=' + json.dumps("Open " + label) + ']'
            self.cli("click", selector)

        conversation()
        self.shot("f05-conversation-markdown")
        click_markdown_reference('"src/Original file.txt:9"')
        self.ready('document.querySelector(".files .crumbs strong")?.textContent==="Original file.txt" && document.querySelector(".files .fstatus")?.textContent.includes("Ln 9,")')
        self.assertTrue(self.cli("eval", 'document.querySelector(".files .cm-editor").checkVisibility()'))
        self.shot("f05-conversation-file-line")
        conversation()
        click_markdown_reference("#12")
        click_markdown_reference("acme/other#12")
        self.ready('referenceRequests.some(request=>request.url==="https://example.com/acme/base/-/issues/12") && referenceRequests.some(request=>request.url==="https://example.com/acme/other/-/issues/12")')
        self.assertFalse(self.cli("eval", 'referenceRequests.some(request=>request.url==="https://example.com/acme/app/-/issues/12")'))
        click_markdown_reference("#13")
        self.ready('document.querySelector(".cmenu")?.textContent.includes("acme/app") && document.querySelector(".cmenu")?.textContent.includes("acme/base")')
        self.assertFalse(self.cli("eval", 'referenceRequests.some(request=>request.url.endsWith("/issues/13"))'))
        self.shot("f05-conversation-project-choice")
        self.cli("eval", '[...document.querySelectorAll(".cmenu button")].find(b=>b.textContent.includes("acme/base")).click();true')
        self.ready('referenceRequests.some(request=>request.url==="https://example.com/acme/base/-/issues/13")')
