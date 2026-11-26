"""Files reliability net (Files → IDE level, phase 0): the real Files component in a headless browser over a real
piperd and filesystem, driven through the scenarios where the user and an agent edit the same files (K1–K8), plus
the editor reflexes users bring from an IDE. Each test states the behaviour Files must have.

Tests marked ``expectedFailure`` document a known defect (its finding id in the docstring); the fix removes the
marker, so an unexpected success fails the suite until the marker goes. Run with PIPER_BROWSER_TESTS=1."""
import json
import os
import shutil
import subprocess
import threading
import time
import unittest

import _env
import common
from test_workbench97 import git, wait, write

BROWSER = os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser")

FILES = {
    "src/A.java": "class A {\n  int alpha;\n  int beta;\n  int gamma;\n}\n",
    "src/B.java": "class B {\n  String place(int n) { return \"x\"; }\n}\n",
    "src/C.java": "class C {\n  void run() { new B().place(1); }\n}\n",
    "README.md": "# Readme\n",
}
# enough rows that the tree scrolls
FILES["src/Many.java"] = "class Many {\n%s}\n" % "".join("  long many%02d = 0;\n" % i for i in range(60))   # past the per-file cap
FILES.update({"pkg/f%02d/Item%02d.java" % (i, i): "class Item%02d {}\n" % i for i in range(40)})


@unittest.skipUnless(BROWSER, "set PIPER_BROWSER_TESTS=1 for the agent-browser regression")
class FilesReliabilityBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import piperd
        cls.old_env = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        os.environ.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        cls.m = _env.manager(cls)
        cls.root = root = os.path.realpath(_env.make_project("frel", FILES))
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "t@example.com")
        git(root, "config", "user.name", "Tester")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "init")
        cls.key = "team-x/frel"
        _env.register(cls.key, root)
        cls.piperd = piperd
        cls.old = piperd.MANAGER, piperd.PORT
        cls.server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        piperd.MANAGER, piperd.PORT = cls.m, cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.browser = "piper-files-rel-%s" % os.getpid()
        cls.cli("open", piperd.gui_url())
        cls.ready('!!document.querySelector("#actbar")')

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["agent-browser", "--session", cls.browser, "close"], capture_output=True, timeout=20)
        cls.server.shutdown()
        cls.server.server_close()
        cls.piperd.MANAGER, cls.piperd.PORT = cls.old
        for k, v in cls.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ------------------------------------------------------------ helpers
    @classmethod
    def cli(cls, *args, session=None):
        env = None
        if session and os.environ.get("AGENT_BROWSER_PROFILE"):   # piper-sandbox gives one Chrome profile: a second
            env = dict(os.environ, AGENT_BROWSER_PROFILE=os.environ["AGENT_BROWSER_PROFILE"] + "-" + session[-1])   # browser its own
        run = subprocess.run(["agent-browser", "--session", session or cls.browser, "--json", *args],
                             capture_output=True, text=True, timeout=30, env=env)
        out = (run.stdout + run.stderr).replace(common.own_token("gui"), "[token]")
        assert run.returncode == 0, out
        data = json.loads(run.stdout)
        assert data["success"], data
        return data.get("data", {}).get("result")

    @classmethod
    def ready(cls, code, timeout=20):
        assert wait(lambda: cls.cli("eval", code) is True, timeout=timeout), code

    def js(self, code, session=None):
        return self.cli("eval", code, session=session)

    def setUp(self):
        git(self.root, "checkout", "-q", "--", ".")
        git(self.root, "clean", "-qfd")
        self.seq = 1
        self.mount()

    def mount(self, session=None):
        # a fresh component per test; fetch passes through window.netDelay (url regex → ms) for latency
        self.js('''(async()=>{
            if(!window.realFetch){window.realFetch=window.fetch;window.netDelay=[];
              window.fetch=async(u,o)=>{const s=String(u);for(const [re,ms] of window.netDelay){
                if(new RegExp(re).test(s)) await new Promise(r=>setTimeout(r,ms));}return window.realFetch(u,o);};}
            window.netDelay=[];window.frelChanged=null;
            const {createFiles}=await import('/piper/dist/files.js');
            window.fileAlerts=[];
            const modal=(title,...content)=>{const box=document.createElement('section');box.id='frel-modal';
              box.append(title,...content.filter(Boolean));document.body.append(box);};
            window.frel=createFiles({toast:t=>window.fileAlerts.push(t),notice:()=>{},modal,
              closeModal:()=>document.querySelector('#frel-modal')?.remove(),
              projects:()=>[%s],changed:()=>window.frelChanged||new Set(),insert:()=>false,diff:()=>{},addNote:()=>false,working:()=>false});
            document.body.replaceChildren(window.frel.el);
            window.frel.el.style.height='600px';
            await window.frel.openDirectory(%s,'src');
            window.frel.sessionEdited(%s,'agent',1,[]);
            return true;})()''' % (json.dumps({"key": self.key, "path": self.root}), json.dumps(self.key),
                                    json.dumps(self.key)), session=session)
        assert wait(lambda: self.js('!!document.querySelector(\'.files [data-nav="src/A.java"]\')', session=session) is True,
                    timeout=20)

    def agent_edit(self, *paths):
        """an agent's Edit/Write (the PostToolUse signal Files gets from the session state)"""
        self.seq += 1
        self.js("window.frel.sessionEdited(%s,'agent',%d,%s); true" % (json.dumps(self.key), self.seq,
                                                                         json.dumps(list(paths))))

    def open(self, rel, needle, session=None):
        self.js("window.frel.open(%s); true" % json.dumps(rel), session=session)
        assert wait(lambda: self.js('document.querySelector(".files .cm-content")?.textContent.includes(%s)===true'
                                    % json.dumps(needle), session=session) is True, timeout=20), rel

    def editor(self, body, session=None):
        """run `body` with `ed` = the shown CodeMirror view"""
        return self.js('''(async()=>{const cm=await (await import('/piper/dist/cmeditor.js')).loadCM();
            const ed=cm.EditorView.findFromDOM(document.querySelector('.files .cm-editor'));%s})()''' % body, session=session)

    def type_at_end(self, text, session=None):
        self.editor("ed.dispatch({changes:{from:ed.state.doc.length,insert:%s},userEvent:'input.type'});return true;"
                    % json.dumps(text), session=session)

    def disk(self, rel):
        with open(os.path.join(self.root, rel)) as f:
            return f.read()

    def text(self, session=None):
        return self.js('document.querySelector(".files .cm-content").textContent', session=session)

    def banner(self, session=None):
        return self.js('Array.from(document.querySelectorAll(".files .fbanner")).map(b=>b.textContent).join("|")', session=session)

    # ------------------------------------------------------------ K1–K8
    def test_k1_agent_edit_refreshes_a_clean_editor_in_place(self):
        """K1 / B19: an agent edit of an open, clean file shows up within 1 s; the caret stays on its line."""
        self.open("src/A.java", "alpha")
        self.editor("ed.dispatch({selection:{anchor:ed.state.doc.line(3).from+4}});return true;")
        write(self.root, "src/A.java", FILES["src/A.java"].replace("alpha", "alpha2"))
        self.agent_edit("src/A.java")
        self.ready('document.querySelector(".files .cm-content").textContent.includes("alpha2")', timeout=1.5)
        line = self.editor("const h=ed.state.selection.main.head;return [ed.state.doc.lineAt(h).number,"
                           "h-ed.state.doc.lineAt(h).from];")
        self.assertEqual(line, [3, 4], "caret moved after an agent edit (B19)")

    def test_k2_agent_edit_of_a_dirty_editor_keeps_the_buffer(self):
        """K2: the user's unsaved text survives an agent edit; nothing is written; a conflict is offered."""
        self.open("src/A.java", "alpha")
        self.type_at_end("// MINE\n")
        write(self.root, "src/A.java", FILES["src/A.java"].replace("alpha", "theirs"))
        self.agent_edit("src/A.java")
        self.ready('document.querySelectorAll(".files .fbanner button").length>0')
        self.assertIn("// MINE", self.text())
        self.assertIn("theirs", self.disk("src/A.java"))
        self.assertNotIn("// MINE", self.disk("src/A.java"))

    def test_k3_out_of_band_change_reaches_a_clean_editor(self):
        """K3 / B1 / UX#6: a change without an agent edit event (Bash sed, git, IntelliJ) reaches a clean,
        shown editor within 3 s."""
        self.open("src/A.java", "alpha")
        write(self.root, "src/A.java", FILES["src/A.java"].replace("alpha", "sedded"))
        self.ready('document.querySelector(".files .cm-content").textContent.includes("sedded")', timeout=3.5)

    def test_k3_out_of_band_new_file_appears_in_the_tree(self):
        """K3 / B1: a file created outside Piper in an expanded folder shows in the tree within 3 s."""
        write(self.root, "src/Generated.java", "class Generated {}\n")
        self.ready('!!document.querySelector(\'.files [data-nav="src/Generated.java"]\')', timeout=3.5)

    def test_k4_vanished_file_offers_save_as_not_overwrite(self):
        """K4 / B3: a file deleted on disk while edited is shown as gone with Save as…; never an Overwrite
        that cannot succeed."""
        self.open("src/A.java", "alpha")
        self.type_at_end("// keep me\n")
        os.unlink(os.path.join(self.root, "src/A.java"))
        self.js("window.frel.save(); true")
        self.ready('document.querySelectorAll(".files .fbanner button").length>0')
        labels = self.js('Array.from(document.querySelectorAll(".files .fbanner button")).map(b=>b.textContent)')
        self.assertIn("// keep me", self.text())
        self.assertFalse(any("Overwrite" in x for x in labels), labels)
        self.assertTrue(any("Save as" in x for x in labels), labels)

    def test_k6_save_while_a_save_is_in_flight_is_no_conflict(self):
        """K6 / B4: typing and ⌘S while the previous write is still running ends with disk = buffer and no
        conflict banner (writes are queued)."""
        self.open("src/A.java", "alpha")
        self.js('window.netDelay=[["/api/fs/write",700]]; true')
        self.type_at_end("// one\n")
        self.js("window.frel.save(); true")
        time.sleep(0.2)
        self.type_at_end("// two\n")
        self.js("window.frel.save(); true")
        self.assertTrue(wait(lambda: "// two" in self.disk("src/A.java"), timeout=5), self.banner())
        self.assertEqual(self.banner(), "")
        # the editor holds the text without the file's final newline; the write puts it back
        self.assertEqual(self.disk("src/A.java"), self.text_raw() + "\n")

    def text_raw(self):
        return self.editor("return ed.state.doc.toString();")

    def test_k7_concurrent_user_and_agent_write_never_loses_text(self):
        """K7: the agent writes between the user's open and save: the sha lock refuses the save, the user's text
        stays in the editor and the agent's on disk."""
        self.open("src/A.java", "alpha")
        write(self.root, "src/A.java", FILES["src/A.java"].replace("alpha", "agent"))
        self.type_at_end("// user\n")
        self.js("window.frel.save(); true")
        self.ready('document.querySelectorAll(".files .fbanner button").length>0')
        self.assertIn("// user", self.text())
        self.assertIn("agent", self.disk("src/A.java"))

    def test_k8_tree_keeps_scroll_and_focus_under_agent_edits(self):
        """K8 / B2: agent edits do not blank the tree ("loading…"), scroll it to the top or drop its focus."""
        self.js('''(async()=>{for(let i=0;i<40;i++){await window.frel.openDirectory(%s,'pkg/f'+String(i).padStart(2,'0'));}
            return true;})()''' % json.dumps(self.key))
        self.ready('!!document.querySelector(\'.files [data-nav="pkg/f39/Item39.java"]\')')
        self.js('''(()=>{const t=document.querySelector(".files .tree");t.scrollTop=t.scrollHeight;
            document.querySelector('.files [data-nav="pkg/f39/Item39.java"]').focus();
            window.sawLoading=false;window.treeObs=new MutationObserver(()=>{
              if(Array.from(document.querySelectorAll(".files .tree .tnode")).some(n=>n.textContent==="loading…"))window.sawLoading=true;});
            window.treeObs.observe(document.querySelector(".files .fleft"),{childList:true,subtree:true});return true;})()''')
        before = self.js('document.querySelector(".files .tree").scrollTop')
        self.assertGreater(before, 0)
        for _ in range(3):
            write(self.root, "src/A.java", "class A { int x%d; }\n" % self.seq)
            self.agent_edit("src/A.java")
            time.sleep(0.3)
        time.sleep(0.5)
        state = self.js('''({loading:window.sawLoading,scroll:document.querySelector(".files .tree").scrollTop,
            focus:document.activeElement?.getAttribute("data-nav")})''')
        self.assertEqual(state, {"loading": False, "scroll": before, "focus": "pkg/f39/Item39.java"})

    def test_k5_branch_switch_with_open_tabs_keeps_tree_and_tabs_consistent(self):
        """K5 / B1 / B3: `git checkout` of another branch with ten tabs open (one with unsaved text): within 3 s the
        clean tab shows the branch's text, a file the branch lacks is gone (#s4), the tree matches the branch without
        blanking, and the unsaved text is kept under a conflict - never written."""
        git(self.root, "checkout", "-q", "-b", "k5")
        write(self.root, "src/A.java", FILES["src/A.java"].replace("alpha", "branched"))
        write(self.root, "src/C.java", "class C {\n  void branch() {}\n}\n")
        write(self.root, "src/OnBranch.java", "class OnBranch {}\n")
        os.unlink(os.path.join(self.root, "src/B.java"))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "k5")
        git(self.root, "checkout", "-q", "main")

        def back():
            git(self.root, "checkout", "-q", "-f", "main")
            git(self.root, "branch", "-q", "-D", "k5")
        self.addCleanup(back)
        for rel in ["pkg/f%02d/Item%02d.java" % (i, i) for i in range(6)] + ["README.md", "src/B.java", "src/A.java"]:
            self.js("window.frel.open(%s); true" % json.dumps(rel))
            self.ready('!!document.querySelector(%s)' % json.dumps('.files .ftab[title^="%s"]' % rel))
        # the shown tab holds unsaved text (leaving a tab saves it): the checkout comes before its autosave
        self.open("src/C.java", "run")
        self.assertEqual(self.js('document.querySelectorAll(".files .ftab").length'), 10)
        self.js('''window.sawLoading=false;window.treeObs=new MutationObserver(()=>{
            if(Array.from(document.querySelectorAll(".files .tree .tnode")).some(n=>n.textContent==="loading…"))window.sawLoading=true;});
            window.treeObs.observe(document.querySelector(".files .fleft"),{childList:true,subtree:true});true''')
        self.type_at_end("// unsaved\n")
        git(self.root, "checkout", "-q", "k5")
        self.ready('document.querySelector(".files .fbanner.b-conflict")!==null', timeout=3.5)
        self.ready('!!document.querySelector(\'.files [data-nav="src/OnBranch.java"]\') && '
                   '!document.querySelector(\'.files [data-nav="src/B.java"]\')', timeout=3.5)
        self.ready('!!document.querySelector(\'.files .ftab.gone[title^="src/B.java"]\')', timeout=3.5)
        self.assertFalse(self.js("window.sawLoading"), "the tree blanked during the branch switch (B2)")
        self.assertIn("// unsaved", self.text())
        self.assertNotIn("// unsaved", self.disk("src/C.java"))
        self.assertIn("branch()", self.disk("src/C.java"))
        self.js("window.frel.open('src/A.java'); true")   # a clean tab in the background already reads the branch
        self.ready('document.querySelector(".files .cm-content").textContent.includes("branched")', timeout=1.5)
        self.js("window.frel.open('src/B.java'); true")
        self.ready('document.querySelector(".files .fbanner.b-missing")!==null')
        self.assertIn("Save as", self.banner())
        self.assertNotIn("Overwrite", self.banner())

    def test_k10_two_clients_see_each_other_and_never_overwrite_silently(self):
        """K10: two browsers on the same file. A save in one reaches the other's clean editor within 3 s; when both
        have unsaved text, the second save is refused with a conflict - the first one's text stays on disk."""
        other = self.browser + "-2"
        self.cli("open", self.piperd.gui_url(), session=other)
        self.addCleanup(lambda: subprocess.run(["agent-browser", "--session", other, "close"], capture_output=True, timeout=20))
        assert wait(lambda: self.cli("eval", '!!document.querySelector("#actbar")', session=other) is True, timeout=20)
        self.mount(session=other)
        self.open("src/A.java", "alpha")
        self.open("src/A.java", "alpha", session=other)
        self.type_at_end("// from one\n")
        self.js("window.frel.save(); true")
        self.assertTrue(wait(lambda: "// from one" in self.disk("src/A.java"), timeout=5), self.banner())
        self.assertTrue(wait(lambda: "// from one" in self.text(session=other), timeout=3.5), "the other client did not refresh")
        # both edit now; the first saves, the second must not overwrite it silently
        self.type_at_end("// one again\n")
        self.type_at_end("// two\n", session=other)
        self.js("window.frel.save(); true")
        self.assertTrue(wait(lambda: "// one again" in self.disk("src/A.java"), timeout=5), self.banner())
        self.js("window.frel.save(); true", session=other)
        self.assertTrue(wait(lambda: "b-conflict" in self.js(
            'Array.from(document.querySelectorAll(".files .fbanner")).map(b=>b.className).join(" ")', session=other), timeout=4),
            self.banner(session=other))
        self.assertIn("// two", self.text(session=other))
        self.assertIn("// one again", self.disk("src/A.java"))
        self.assertNotIn("// two", self.disk("src/A.java"))

    # ------------------------------------------------------------ editor reflexes
    def test_b5_editor_keeps_multiple_carets(self):
        """B5 / UX#2: the editor allows several selections (⌘D next occurrence, ⌥-click add caret)."""
        self.open("src/A.java", "alpha")
        n = self.editor('''const s=ed.state.doc.line(2).from, t=ed.state.doc.line(3).from;
            ed.dispatch({selection:cm.EditorSelection.create([cm.EditorSelection.cursor(s),cm.EditorSelection.cursor(t)])});
            return ed.state.selection.ranges.length;''')
        self.assertEqual(n, 2)

    def test_s1_autosave_survives_a_diff_round_trip(self):
        """S1 / UX#7: an edit pending autosave is saved after the diff view is opened and closed again
        (the timer fires while the diff is shown)."""
        self.open("src/A.java", "alpha")
        self.type_at_end("// pending\n")
        self.js("window.frel.toggleDiff(); true")
        time.sleep(2.6)
        self.js("window.frel.toggleDiff(); true")
        self.assertTrue(wait(lambda: "// pending" in self.disk("src/A.java"), timeout=4),
                        self.js('document.querySelector(".files .wstate")?.textContent'))

    # ------------------------------------------------------------ project search
    def search(self, q):
        self.js('(()=>{const i=document.querySelector(".files input.sq");i.value=%s;'
                'i.dispatchEvent(new Event("input",{bubbles:true}));'
                'Array.from(i.parentElement.querySelectorAll("button")).find(b=>b.textContent==="Search").click();'
                'return true;})()' % json.dumps(q))

    def results(self):
        return self.js('document.querySelector(".files .pbody")?.textContent||""')

    def test_b7_an_older_slow_search_never_replaces_a_newer_one(self):
        """B7: a slow search answered after a newer one does not overwrite the newer results."""
        self.js('window.netDelay=[["/api/fs/grep.*alpha",1500]]; window.frel.searchMode(); true')
        self.ready('!!document.querySelector(".files input.sq")')
        self.search("alpha")
        time.sleep(0.2)
        self.search("gamma")
        time.sleep(2.5)
        shown = self.results()
        self.assertIn("int gamma", shown)
        self.assertNotIn("int alpha", shown)

    def test_b9_project_search_is_literal_by_default(self):
        """B9 / UX#3: typing `place(` finds the call, it is not a broken regex."""
        self.js('window.frel.searchMode(); true')
        self.ready('!!document.querySelector(".files input.sq")')
        self.search("place(")
        self.ready('(document.querySelector(".files .pbody")?.textContent||"").includes("C.java")', timeout=5)

    def test_b9_a_broken_regex_says_so_at_the_field(self):
        """B9: with .* on, `place(` is an invalid regex - the error sits under the field, never "0 matches"."""
        self.js('window.frel.searchMode(); true')
        self.ready('!!document.querySelector(".files .stog [data-opt=rx]")')
        self.js('document.querySelector(".files .stog [data-opt=rx]").click(); true')
        self.ready('document.querySelector(".files .stog [data-opt=rx]").getAttribute("aria-pressed")==="true"')
        self.search("place(")
        self.ready('!!document.querySelector(".files .pbody .serr")', timeout=5)
        shown = self.results()
        self.assertIn("Invalid regex", shown)
        self.assertEqual(shown.lower().count("invalid regex"), 1, shown)
        self.assertNotIn("0 matches", shown)

    # ------------------------------------------------------------ 1e tabs
    def tab_menu(self, rel):
        """open the context menu of the tab of `rel`; its item labels"""
        self.js('document.querySelector(%s).dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,clientX:200,clientY:60}));true'
                % json.dumps('.files .ftab[title^="%s"]' % rel))
        self.ready('!!document.querySelector(".cmenu")')
        return self.js('Array.from(document.querySelectorAll(".cmenu button[role^=menuitem]")).map(b=>b.querySelector(".grow").textContent)')

    def pick(self, label):
        self.js('Array.from(document.querySelectorAll(".cmenu button[role^=menuitem]")).find(b=>b.querySelector(".grow").textContent===%s).click();true'
                % json.dumps(label))

    def tabs(self):
        return self.js('Array.from(document.querySelectorAll(".files .ftab")).map(t=>t.title.split(" ")[0])')

    def test_1e_tab_menu_closes_tabs_and_never_drops_unsaved_ones(self):
        """1e / UX#8-9: the tab menu closes (no file operations); an unsaved tab shows one dot and asks first."""
        # the autosave never lands: C stays unsaved; B has git changes
        self.js('window.netDelay=[["/api/fs/write",1e9]]; window.frelChanged=new Set(["src/B.java"]); true')
        for rel, needle in (("src/A.java", "alpha"), ("src/B.java", "place"), ("pkg/f00/Item00.java", "Item00"), ("src/C.java", "run")):
            self.open(rel, needle)
        self.type_at_end("// mine\n")
        self.ready('!!document.querySelector(\'.files .ftab.dirty[title^="src/C.java"]\')')
        dirty = self.js('(()=>{const t=document.querySelector(\'.files .ftab.dirty\');'
                        'return {close:t.querySelector(".fclose").textContent,dots:t.querySelectorAll(".gdot").length}})()')
        self.assertEqual(dirty, {"close": "●", "dots": 0})
        self.assertTrue(self.js('document.querySelector(\'.files .ftab[title^="src/B.java"]\').classList.contains("gs-mod")'))
        warn = self.js('(()=>{const p=document.createElement("i");p.style.color="var(--warn)";document.body.append(p);'
                       'const c=getComputedStyle(p).color;p.remove();return c})()')
        self.assertEqual(self.js('getComputedStyle(document.querySelector(\'.files .ftab[title^="src/B.java"] .tl\')).color'), warn)
        self.assertEqual(self.tab_menu("src/A.java"), ["Close", "Close Others", "Close All", "Close Unmodified", "Copy Path"])
        self.pick("Close Unmodified")   # A and Item00 have neither unsaved edits nor git changes
        self.ready('document.querySelectorAll(".files .ftab").length===2')
        self.assertEqual(self.tabs(), ["src/B.java", "src/C.java"])
        self.tab_menu("src/B.java")
        self.pick("Close Others")
        self.ready('!!document.querySelector("#frel-modal")')   # C is unsaved: asked, not dropped
        self.js('Array.from(document.querySelectorAll("#frel-modal button")).find(b=>b.textContent==="Cancel").click();true')
        self.assertEqual(self.tabs(), ["src/B.java", "src/C.java"])
        self.assertIn("// mine", self.text())
        self.tab_menu("src/C.java")
        self.pick("Close All")
        self.ready('!!document.querySelector("#frel-modal")')
        self.js('Array.from(document.querySelectorAll("#frel-modal button")).find(b=>b.textContent==="Discard").click();true')
        self.ready('document.querySelectorAll(".files .ftab").length===0')

    # ------------------------------------------------------------ 1c keys and reflexes
    def click_at(self, x, y, mods="-"):
        """a real mouse click (Chrome's input pipeline) with modifiers alt / meta / ctrl / shift"""
        run = subprocess.run(["agent-browser", "--session", self.browser, "--json", "get", "cdp-url"], capture_output=True, text=True, timeout=30)
        data = json.loads(run.stdout)["data"]
        cdp = data.get("url") or data.get("result") or data.get("cdpUrl")
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cdpinput.mjs")
        out = subprocess.run(["node", here, cdp, "127.0.0.1:%d" % self.server.server_port, str(x), str(y), mods],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)

    def line_end(self, n):
        """screen point just right of line n's text (a click there puts the caret at its end)"""
        return self.editor("const r=ed.coordsAtPos(ed.state.doc.line(%d).to);return [Math.round(r.right+30),Math.round((r.top+r.bottom)/2)];" % n)

    def word_at(self, n, word):
        return self.editor("const l=ed.state.doc.line(%d);const r=ed.coordsAtPos(l.from+l.text.indexOf(%s)+1);"
                           "return [Math.round(r.left+2),Math.round((r.top+r.bottom)/2)];" % (n, json.dumps(word)))

    def test_1c_alt_click_adds_a_caret_and_typing_writes_at_both(self):
        """B5 / UX#2: ⌥+click adds a caret (no declaration jump); typed text lands at every caret."""
        self.open("src/A.java", "alpha")
        self.click_at(*self.line_end(2))
        self.click_at(*self.line_end(3), mods="alt")
        self.assertEqual(self.editor("return ed.state.selection.ranges.length;"), 2)
        self.cli("keyboard", "type", "Q")
        self.assertEqual(self.editor("return [ed.state.doc.line(2).text, ed.state.doc.line(3).text];"), ["  int alpha;Q", "  int beta;Q"])

    def test_1c_cmd_click_goes_to_the_declaration_on_click(self):
        """R11: ⌘+click on a name opens its declaration; the ⌘ mousedown adds no caret."""
        self.open("src/C.java", "run")
        self.click_at(*self.word_at(2, "place"), mods="meta")
        self.ready('document.querySelector(".files .ftab.on")?.title.startsWith("src/B.java")===true')

    def press_of(self, name):
        parts = name.split("-")
        key = {"BracketLeft": "[", "BracketRight": "]"}.get(parts[-1], parts[-1])
        key = key[3:].lower() if key.startswith("Key") else key
        mods = {"Ctrl": "Control", "Mod": "Meta", "Alt": "Alt", "Shift": "Shift"}
        return "+".join([mods[m] for m in parts[:-1]] + [key])

    def test_1c_every_keys_row_runs_its_binding_and_stays_in_files(self):
        """UX#10: each IntelliJ key row of the Keys dialog fires its binding from the editor - none types a character,
        none leaves Files; ⌘E also works from the tree."""
        rows = self.js('(async()=>{const {FILE_KEYS}=await import("/piper/dist/shortcuts.js");'
                       'return FILE_KEYS.intellij.filter(r=>r.keys.length).map(r=>({id:r.id,keys:r.keys}))})()')
        self.assertGreater(len(rows), 12)
        self.js('window.fk=[];window.frel.el.addEventListener("piper-filekey",e=>fk.push(e.detail.id));true')
        self.open("src/B.java", "place")
        self.open("src/A.java", "alpha")
        for row in rows:
            for key in row["keys"]:
                if row["id"] == "closeTab":
                    continue   # last: it closes the tab
                self.click_at(*self.line_end(3))
                before = self.js("fk.length")
                self.cli("press", self.press_of(key))
                self.ready("fk.length===%d" % (before + 1), timeout=5)
                self.assertEqual(self.js("fk[fk.length-1]"), row["id"], key)
                self.js('document.querySelector("#frel-modal")?.remove();true')
                self.cli("press", "Escape")
                # tab switching moved away from A: back to it
                self.js("window.frel.open('src/A.java');true")
                self.ready('document.querySelector(".files .ftab.on")?.title.startsWith("src/A.java")===true')
        self.assertNotIn("∑", self.text())
        # ⌘E from the tree as well; then ⌥W / ⌘W close the tab without typing
        self.js('document.querySelector(\'.files [data-nav="src/A.java"]\').focus();true')
        self.cli("press", "Meta+e")
        self.ready('document.querySelector("#frel-modal")?.textContent.includes("Recent files")===true')
        self.assertTrue(self.js('document.querySelector("#frel-modal .pitem")?.textContent.startsWith("B.java")'), "the previous file first")
        self.js('document.querySelector("#frel-modal")?.remove();true')
        for key in ("Alt+w", "Meta+w"):
            n = self.js('document.querySelectorAll(".files .ftab").length')
            self.click_at(*self.line_end(3))
            self.cli("press", key)
            self.ready('document.querySelectorAll(".files .ftab").length===%d' % (n - 1))

    def test_1c_intellij_editing_keys_do_what_they_say(self):
        """1c: ⌥↑/⌥↓ extend and shrink the selection, ⌃⌘G selects every occurrence, ⌘D duplicates, ⌥⇧↓ moves, ⌘⌫ deletes."""
        self.open("src/A.java", "alpha")
        sel = lambda: self.editor("return ed.state.selection.ranges.map(r=>ed.state.sliceDoc(r.from,r.to));")
        line = lambda n: self.editor("return ed.state.doc.line(%d).text;" % n)
        self.click_at(*self.word_at(2, "alpha"))
        self.cli("press", "Alt+ArrowUp")
        self.assertEqual(sel(), ["alpha"])
        self.cli("press", "Alt+ArrowUp")
        self.assertEqual(sel(), ["int alpha;"])
        self.cli("press", "Alt+ArrowDown")
        self.assertEqual(sel(), ["alpha"])
        self.click_at(*self.word_at(2, "int"))
        self.cli("press", "Control+Meta+g")
        self.assertEqual(sel(), ["int", "int", "int"])
        self.click_at(*self.line_end(2))
        self.cli("press", "Meta+d")
        self.assertEqual([line(2), line(3)], ["  int alpha;", "  int alpha;"])
        self.cli("press", "Meta+Backspace")
        self.assertEqual([line(2), line(3)], ["  int alpha;", "  int beta;"])
        self.cli("press", "Alt+Shift+ArrowDown")   # the caret is on beta (the deleted copy's place)
        self.assertEqual([line(2), line(3), line(4)], ["  int alpha;", "  int gamma;", "  int beta;"])

    def test_1c_navigation_keys_do_what_they_say(self):
        """1c: ⌃G adds the next occurrence, ⌘F12 is the structure, ⌘⇧O goes to a file, ⌘⇧] / ⌘⇧[ switch tabs."""
        self.open("src/B.java", "place")
        self.open("src/A.java", "alpha")
        sel = lambda: self.editor("return ed.state.selection.ranges.map(r=>ed.state.sliceDoc(r.from,r.to));")
        self.click_at(*self.word_at(2, "int"))
        self.cli("press", "Control+g")
        self.cli("press", "Control+g")
        self.assertEqual(sel(), ["int", "int"])
        self.click_at(*self.line_end(2))
        self.cli("press", "Meta+F12")
        self.ready('document.querySelector("#frel-modal")?.textContent.includes("Outline A.java")===true')
        self.js('document.querySelector("#frel-modal")?.remove();true')
        self.click_at(*self.line_end(2))
        self.cli("press", "Meta+Shift+o")
        self.ready('!!document.querySelector("#frel-modal input")')
        self.assertNotIn("Outline", self.js('document.querySelector("#frel-modal").textContent'))
        self.js('document.querySelector("#frel-modal")?.remove();true')
        on = 'document.querySelector(".files .ftab.on")?.title.startsWith(%s)===true'
        self.click_at(*self.line_end(2))
        self.cli("press", "Meta+Shift+BracketLeft")
        self.ready(on % json.dumps("src/B.java"))
        self.cli("press", "Meta+Shift+BracketRight")
        self.ready(on % json.dumps("src/A.java"))

    def test_1c_editing_keys_leave_the_find_replace_fields_alone(self):
        """review: ⌘R's Replace field sits inside the editor - ⌘⌫, ⌘D and ⌥⇧↑ there edit the field, never the file."""
        self.open("src/A.java", "alpha")
        text = lambda: self.editor("return ed.state.doc.toString();")
        before = text()
        self.click_at(*self.line_end(2))
        self.cli("press", "Meta+r")
        self.ready('document.activeElement?.name==="replace"')
        self.cli("keyboard", "type", "zz")
        for key in ("Meta+Backspace", "Meta+d", "Alt+Shift+ArrowUp", "Alt+ArrowUp"):
            self.cli("press", key)
        self.assertEqual(text(), before)

    def test_1d_more_in_this_file_reads_the_listed_query(self):
        """S2: a file past 50 matches says "+ n more"; the button fetches that query's rest even after the field changed."""
        self.js("window.frel.searchMode(); true")
        self.ready('!!document.querySelector(".files input.sq")')
        self.search("many")
        self.ready('!!document.querySelector(".files .pbody button.hmore")')
        self.assertIn("11 more", self.js('document.querySelector(".files .pbody button.hmore").textContent'))
        self.js('(()=>{const i=document.querySelector(".files input.sq");i.value="alpha";'
                'i.dispatchEvent(new Event("input",{bubbles:true}));return true;})()')
        self.js('document.querySelector(".files .pbody button.hmore").click();true')
        self.ready('!document.querySelector(".files .pbody button.hmore")')
        shown = self.results()
        self.assertIn("many59", shown)
        self.assertNotIn("int alpha", shown)
        self.assertEqual(self.js('document.querySelectorAll(".files .pbody button.hit:not(.hmore)").length'), 61)   # "class Many" too: case-insensitive

if __name__ == "__main__":
    unittest.main()
