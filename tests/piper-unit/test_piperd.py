"""piperd HTTP + WebSocket gates, served over socketpairs (no port is bound)."""
import base64
import json
import os
import re
import socket
import struct
import threading
import time
import unittest
from unittest import mock

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)
import common
import piperd
import wsproto

PORT = int(os.environ["PIPER_PORT"])
HOST = "127.0.0.1:%d" % PORT


class FakeManager:
    def __init__(self):
        self.created = []
        self.polled = 0

    def gui_polled(self):
        self.polled += 1

    def list(self):
        return [{"id": "s1", "kind": "project", "state": "ready"}]

    def pending_list(self):
        return []

    def pending_gone_list(self):   # F17 (9.63): the "cancelled - nothing was sent" rows
        return []

    def inbox_summary(self):
        return {"v": 1, "status": "no-token", "new": 0, "total": 0, "needs": 0}

    def prod_summary(self):
        return {"v": 1, "status": "idle", "new": 0, "on": False}

    def feed_summary(self):
        return {"on": False}

    def queue_summary(self):
        return {"actions": []}

    def start_opts_default(self):
        return []

    rebuild: dict = {}

    def image_rebuild(self):
        return dict(self.rebuild)

    notice = None

    def interrupted_notice(self):   # 9.141
        return self.notice

    def set_image_rebuild(self, on):
        self.rebuild = {"at": 1.0} if on else {}
        self.created.append(("image-rebuild", on))
        return {"image_rebuild": self.image_rebuild()}

    def prod(self):
        return {"status": "idle", "report": None}

    def prod_run_now(self, mode):
        if mode != "check":
            raise ValueError("mode must be check or suggest")
        self.created.append(("prod-run", mode))
        return {"session": "p1", "mode": mode}

    def get(self, sid):
        if sid != "s1":
            raise KeyError(sid)
        return object()

    def create(self, project, task, title, origin, opts=None, agent="claude", image_rebuild=False):
        if project == "nope":
            raise KeyError("unknown project")
        self.created.append((project, task, origin) + ((opts,) if opts else ()) + ((agent,) if agent != "claude" else ())
                            + (("image_rebuild",) if image_rebuild else ()))

        class S:
            def to_json(self):
                return {"id": "new"}
        return S()

    def gitlab_host(self):
        return "gitlab.example"

    def git_status(self, sid):
        self.get(sid)
        return {"branch": "main", "files": []}

    def git_diff(self, sid, file, untracked):
        return "diff for %s untracked=%s" % (file, untracked)

    def git_commit(self, sid, message, files, reviewed=None, amend=False):
        self.created.append(("commit", message, tuple(files)))
        return {"commit": "abc feat", "hooks_disabled": False}

    def fs(self, key, op, args):
        import projectfs
        if key != "team-x/app":
            raise KeyError("unknown project %r" % key)
        if args.get("path") == "secret":
            raise projectfs.FsError("hidden")
        if args.get("path") == "big.log":
            raise projectfs.TooLarge("big.log is larger than 2 MiB - not opened")
        if args.get("path") == "a.png":
            raise projectfs.Binary("a.png looks binary - not opened as text")
        self.created.append(("fs", key, op, args.get("path")))
        return {"op": op}

    def issues(self, key, search, state, gl):
        import gitlab
        raise gitlab.GitLabError(503, "no GitLab token")

    def issue(self, key, iid, gl):
        return {"iid": iid}

    def conversation(self, sid):
        self.get(sid)
        return {"turns": [], "usage": None}

    def search_conversations(self, q):
        if len(q) < 2:
            raise ValueError("short")
        return []

    def board(self):
        return [{"id": "s1", "last": "", "changed": 0}]

    def audit_log(self, sid, limit):
        return [{"event": "x", "session": sid, "limit": limit}]

    def send(self, sid, text):
        self.get(sid)
        if not text:
            raise ValueError("empty")
        self.created.append(("send", sid, text))

    def retry(self, sid):
        self.get(sid)

    stop = approve = forget = retry

    def allow_domain(self, sid, domain):
        if domain == "gitlab.com":
            raise ValueError("served by piperd only")
        return {"domain": domain, "written": True}

    def drop(self, sid, ctype, data):
        if not ctype.startswith("image/"):
            raise ValueError("only images")
        return "/home/.claude/piper-drop/x.png"

    # ---- 9.7 (workbench.py)
    def _visible(self, key, path):
        import projectfs
        if key != "team-x/app":
            raise KeyError(key)
        if path.startswith("secret"):
            raise projectfs.FsError("hidden")

    def fs_marks(self, key, path):
        self._visible(key, path)
        return {"path": path, "added": [[1, 2]], "changed": [], "deleted": [], "untracked": False}

    def fs_symbols(self, key, name, q):
        self._visible(key, "")
        if not name and not q:
            raise ValueError("name or q")
        return {"symbols": [{"name": name or q}], "truncated": False, "indexed": 1}

    def fs_raw(self, key, path):
        self._visible(key, path)
        if not path.endswith((".png", ".svg")):
            raise ValueError("not an image")
        return ("image/svg+xml" if path.endswith(".svg") else "image/png"), b"\x89PNG" + b"\0" * 2000

    def fs_write(self, key, path, text, sha):
        self._visible(key, path)
        if sha != "ok":
            raise ValueError("changed since you opened it")
        self.created.append(("write", path, len(text)))
        return {"sha": "new", "total_lines": text.count("\n")}

    # / L8 / L9
    def fs_create(self, key, path, kind, text):
        self._visible(key, path)
        if path == "exists":
            raise ValueError("exists already exists")
        self.created.append(("create", path, kind, text))
        return {"path": path, "sha": "s"} if kind == "file" else {"path": path}

    def fs_rename(self, key, src, dst):
        self._visible(key, src)
        self._visible(key, dst)
        self.created.append(("rename", src, dst))
        return {"from": src, "to": dst}

    def fs_delete(self, key, path):
        self._visible(key, path)
        self.created.append(("delete", path))
        return {"trash": "20261006-120000-0123abcd"}

    def fs_restore(self, key, trash):
        self._visible(key, "a")
        if trash != "20261006-120000-0123abcd":
            import projectfs
            raise projectfs.FsError("unknown trash entry")
        return {"path": "a"}

    def turn_file_history(self, sid, path):
        self.get(sid)
        self._visible("team-x/app", path)
        return {"turns": [{"index": 0, "add": 1, "del": 0}], "diff_path": path}

    def git_file_diff(self, key, path):
        self._visible(key, path)
        return "diff --git a/%s b/%s\n" % (path, path)

    def git_log(self, key, path, n):
        self._visible(key, path)
        return {"commits": [{"hash": "h", "n": n}]}

    def git_blame(self, key, path):
        self._visible(key, path)
        return {"lines": []}

    def git_graph(self, key, n):
        return {"lines": ["* x %d" % n]}

    def project_git_status(self, key):
        self._visible(key, "")
        return {"root": "/r", "branch": "main", "upstream": "", "ahead": 0, "behind": 0, "files": []}

    def git_show(self, key, commit):
        import gitops
        if commit != "abcd":
            raise gitops.GitError("not a commit")
        return "commit abcd\n"

    def git_ci(self, key):
        return {"project": key, "branch": "main", "pipeline": None, "mr": None}

    def mr_draft(self, key, target):
        return {"project": key, "source": "f", "target": target or "main", "title": "t", "description": ""}

    def create_mr(self, key, title, description, target, draft):
        import gitlab
        if key == "team-x/other":
            raise gitlab.GitLabError(403, "outside the scope")
        if title == "unpushed":
            raise ValueError("push first")
        self.created.append(("mr", title, draft))
        return {"iid": 1, "title": title}

    def stack_status(self, key):
        return {"stack": None, "services": [], "error": "the dev stack is down"}

    def stack_logs(self, key, service, tail):
        return "log %s %d\n" % (service, tail)

    def stack_restart(self, key, service):
        if service == "bad name":
            raise ValueError("invalid service")
        return {"ok": True, "job": "j1"}

    def stack_view(self, key):
        return self.stack_status(key)

    def stack_action(self, key, verb, service):
        if verb not in ("start", "stop", "restart", "down", "up", "build", "rebuild"):
            raise ValueError("verb not offered")
        return {"ok": True, "job": {"id": "j2", "verb": verb}}

    def stack_job_log(self, key, jid, offset):
        if jid != "abcdef123456":
            raise KeyError("no such job of this stack")
        return {"job": {"id": jid}, "offset": offset + 3, "chunk": "ok\n", "state": "running"}

    def stack_job_cancel(self, key, jid):
        return {"ok": True, "state": "running", "signalled": True}

    def turns(self, sid):
        self.get(sid)
        return {"turns": [{"n": 1}]}

    def turn_files(self, sid, frm, to):
        self.get(sid)
        if frm != "t1":
            raise ValueError("unknown snapshot")
        return {"from": frm, "to": to or "now", "files": []}

    def turn_diff(self, sid, frm, to, path):
        self.get(sid)
        return "turn diff %s\n" % path

    def diagnostics(self, sid):
        self.get(sid)
        return {"at": None, "source": "", "items": []}

    def idea_status(self):
        return {"available": False, "url": "http://127.0.0.1:64342/stream", "note": "x"}

    def idea_problems(self, key, path):
        self._visible(key, path)
        return {"available": False, "problems": []}

    def idea_symbol(self, key, path, line, col):
        self._visible(key, path)
        return {"available": True, "info": "%d:%d" % (line, col)}

    def usage(self):
        return {"days": [], "projects": [], "total": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}}

    # ---- 9.12 E: Mattermost + the half-written prompt
    def presence(self, visible):
        self.created.append(("presence", visible))

    def mm_status(self):
        return {"configured": False, "mode": "webhook"}

    def mm_test(self):
        raise ValueError("Mattermost is not configured")

    def input_clear(self, sid):
        self.get(sid)
        self.created.append(("input-clear", sid))


def serve(a: socket.socket) -> None:
    """What socketserver does around a handler: run it, then close the connection."""
    try:
        piperd.Handler(a, ("127.0.0.1", 1), None)
    finally:
        a.close()


def request(raw: bytes) -> tuple[int, dict, bytes]:
    a, b = socket.socketpair()
    b.settimeout(10)
    threading.Thread(target=serve, args=(a,), daemon=True).start()
    b.sendall(raw)
    try:
        b.shutdown(socket.SHUT_WR)
    except OSError:
        pass   # the handler may already have answered and closed (413, 421)
    data = b""
    while True:
        chunk = b.recv(65536)
        if not chunk:
            break
        data += chunk
    b.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    code = int(lines[0].split()[1])
    headers = {k.lower(): v for k, v in (l.split(": ", 1) for l in lines[1:] if ": " in l)}
    return code, headers, body


def get(path, host=HOST, token=None, extra=""):
    auth = "Authorization: Bearer %s\r\n" % token if token else ""
    return request(("GET %s HTTP/1.1\r\nHost: %s\r\n%s%sConnection: close\r\n\r\n" % (path, host, auth, extra)).encode())


def post(path, body, token=None, origin=None):
    raw = json.dumps(body).encode()
    hdr = "POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n" % (path, HOST, len(raw))
    if token:
        hdr += "Authorization: Bearer %s\r\n" % token
    if origin:
        hdr += "Origin: %s\r\n" % origin
    return request((hdr + "Connection: close\r\n\r\n").encode() + raw)


class PiperdHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        piperd.MANAGER = FakeManager()
        cls.tok = common.mint_token("test")

    def test_open_url_selects_chrome_and_validates_before_launch(self):
        token = common.own_token("gui")
        policy = {"open_links_on_host": True, "host_link_browser": "chrome"}
        with mock.patch.object(piperd.MANAGER, "policy", policy, create=True), \
                mock.patch("subprocess.run") as run, mock.patch.object(piperd.sys, "platform", "darwin"):
            run.return_value.returncode = 0
            code, _, _ = post("/api/open-url", {"url": "https://example.test/path?a=1&b=2"}, token)
            self.assertEqual(code, 200)
            self.assertEqual(run.call_args.args[0], ["open", "-a", "Google Chrome", "https://example.test/path?a=1&b=2"])
            policy["host_link_browser"] = "default"
            post("/api/open-url", {"url": "http://localhost:8080"}, token)
            self.assertEqual(run.call_args.args[0], ["open", "http://localhost:8080"])
            run.reset_mock()
            for url in ("file:///tmp/a", "javascript:alert(1)", "https://example.test/\n", "--args"):
                self.assertEqual(post("/api/open-url", {"url": url}, token)[0], 400)
            policy["open_links_on_host"] = False
            self.assertEqual(post("/api/open-url", {"url": "https://example.test"}, token)[0], 403)
            run.assert_not_called()
            policy["open_links_on_host"] = True
            run.return_value.returncode = 1
            self.assertEqual(post("/api/open-url", {"url": "https://example.test"}, token)[0], 502)

    def test_piper_urls_never_launch_an_external_browser(self):
        token = common.own_token("gui")
        policy = {"open_links_on_host": True, "host_link_browser": "chrome"}
        with mock.patch.object(piperd.MANAGER, "policy", policy, create=True), \
                mock.patch.dict(os.environ, {"PIPER_ALLOWED_HOSTS": "piper.example"}), \
                mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            for url in (f"http://localhost:{PORT}/?session=s1", f"http://127.0.0.1:{PORT}/", f"http://[::1]:{PORT}/",
                        "https://piper.example/", "https://piper.example:443/?view=files"):
                self.assertTrue(piperd.is_piper_url(url))
                self.assertEqual(post("/api/open-url", {"url": url}, token)[0], 409)
            run.assert_not_called()
            popen.assert_not_called()
            for url in ("https://piper.example.evil/", "https://piper.example@evil.test/", "http://localhost:8080/"):
                self.assertFalse(piperd.is_piper_url(url))

    def test_artifact_download_is_gui_only_and_always_an_attachment(self):
        token = common.own_token("gui")
        with mock.patch.object(piperd.MANAGER, "fs_download", return_value=b"%PDF-1.7", create=True) as download:
            code, headers, body = get("/api/fs/download?project=demo&path=report.pdf", token=token)
            self.assertEqual((code, body), (200, b"%PDF-1.7"))
            self.assertEqual(headers["content-disposition"], "attachment")
            self.assertEqual(headers["content-type"], "application/octet-stream")
            self.assertIn("sandbox", headers["content-security-policy"])
            download.reset_mock()
            self.assertEqual(get("/api/fs/download?project=demo&path=report.pdf", token=common.mint_token("tty"))[0], 403)
            download.assert_not_called()

    def test_interrupted_card_routes_are_gui_only_and_reach_the_state(self):
        """9.141: the restore card's seen / dismiss need the GUI token; /api/state carries the notice, never to `piper attach`."""
        t = common.own_token("gui")
        limited = common.mint_token("tty")
        notice = {"id": 5.0, "cause": "reboot", "at": 5.0, "sessions": [{"id": "s1", "was": "working", "resumes": True}]}
        with mock.patch.object(piperd.MANAGER, "notice", notice), \
                mock.patch.object(piperd.MANAGER, "interrupted_seen", create=True, return_value={"ok": True}) as seen, \
                mock.patch.object(piperd.MANAGER, "interrupted_dismiss", create=True, return_value={"dismissed": 2}) as dismiss:
            self.assertEqual(json.loads(get("/api/state", token=t)[2])["interrupted_notice"], notice)
            self.assertIsNone(json.loads(get("/api/state", token=limited)[2])["interrupted_notice"])
            self.assertEqual(post("/api/interrupted", {"seen": 5.0}, token=limited)[0], 403)
            self.assertEqual(post("/api/interrupted", {"seen": 5.0}, token=t)[0], 200)
            seen.assert_called_once_with(5.0)
            code, _, body = post("/api/interrupted", {"dismiss": "all"}, token=t)
            self.assertEqual((code, json.loads(body)), (200, {"dismissed": 2}))
            dismiss.assert_called_once_with("all")
            dismiss.side_effect = ValueError('ids must be a list of session ids or "all"')
            self.assertEqual(post("/api/interrupted", {"dismiss": 3}, token=t)[0], 400)

    def test_history_picker_routes_are_gui_only(self):
        from unittest import mock
        t = common.own_token("gui")
        limited = common.mint_token("tty")
        with mock.patch.object(piperd.MANAGER, "conversations", create=True, return_value={"items": []}) as search, \
                mock.patch.object(piperd.MANAGER, "conversation_preview", create=True, return_value={"available": True}) as preview, \
                mock.patch.object(piperd.MANAGER, "resume_conversation", create=True, return_value={"session": "fork-id"}) as resume, \
                mock.patch.object(piperd.MANAGER, "import_conversation", create=True, return_value={"session": "import-id"}) as imported, \
                mock.patch.object(piperd.MANAGER, "continue_session", create=True, return_value={"session": "s1"}) as cont:
            self.assertEqual(post("/api/conversations/s1/continue", {"task": "next"}, token=limited)[0], 403)   # W8
            self.assertEqual(post("/api/conversations/s1/continue", {"task": "next"}, token=t)[0], 200)
            cont.assert_called_once_with("s1", "next")
            for path in ("/api/conversations", "/api/conversations/s1/preview"):
                self.assertEqual(get(path, token=limited)[0], 403)
                self.assertEqual(get(path, token=t)[0], 200)
            self.assertEqual(get("/api/conversations?q=pdf&project=team-x%2Fapp&agent=codex&period=yesterday&offset=50", token=t)[0], 200)
            search.assert_called_with("pdf", "team-x/app", "codex", "yesterday", 50, False)
            self.assertEqual(get("/api/conversations?external=1", token=t)[0], 200)
            search.assert_called_with("", "", "", "", 0, True)
            self.assertEqual(get("/api/conversations?external=yes", token=t)[0], 400)
            preview.assert_called_once_with("s1")
            self.assertEqual(get("/api/conversations?offset=abc", token=t)[0], 400)
            for kind in ("resume", "fork"):
                self.assertEqual(post("/api/conversations/s1/" + kind, {}, token=limited)[0], 403)
                self.assertEqual(post("/api/conversations/s1/" + kind, {}, token=t)[0], 200)
                resume.assert_called_with("s1", kind == "fork")
            self.assertEqual(resume.call_count, 2)
            self.assertEqual(post("/api/conversations/local-id/import", {"project": "team-x/app"}, token=limited)[0], 403)
            self.assertEqual(post("/api/conversations/local-id/import", {"project": "team-x/app", "confirm_checkout": "true"}, token=t)[0], 200)
            imported.assert_called_once_with("local-id", "team-x/app", False, False)
            # R2: hidden paths in the transcript -> 409 with the names; only a real true confirms
            import sessions
            imported.side_effect = sessions.HiddenInTranscript("hidden: a.pem", ["a.pem"])
            code, _, body = post("/api/conversations/local-id/import", {"project": "team-x/app", "confirm_hidden": "true"}, token=t)
            self.assertEqual((code, json.loads(body)["hidden_paths"]), (409, ["a.pem"]))
            imported.assert_called_with("local-id", "team-x/app", False, False)
            imported.side_effect = None
            self.assertEqual(post("/api/conversations/local-id/import", {"project": "team-x/app", "confirm_hidden": True}, token=t)[0], 200)
            imported.assert_called_with("local-id", "team-x/app", False, True)

    def test_switch_agent_route_is_gui_only_and_asks_about_hidden_files(self):
        """9.140: Continue with Claude / Codex - the GUI token only, R2's 409 with the names as an import"""
        from unittest import mock
        import sessions
        t = common.own_token("gui")
        with mock.patch.object(piperd.MANAGER, "switch_agent", create=True,
                               return_value={"session": "new1", "from": "s1", "agent": "claude"}) as switch:
            self.assertEqual(post("/api/sessions/s1/switch-agent", {}, token=common.mint_token("tty"))[0], 403)
            switch.assert_not_called()
            code, _, body = post("/api/sessions/s1/switch-agent", {}, token=t)
            self.assertEqual((code, json.loads(body)["session"]), (200, "new1"))
            switch.assert_called_with("s1", False)
            switch.side_effect = sessions.HiddenInTranscript("hidden: a.pem", ["a.pem"])
            code, _, body = post("/api/sessions/s1/switch-agent", {"confirm_hidden": "true"}, token=t)
            self.assertEqual((code, json.loads(body)["hidden_paths"]), (409, ["a.pem"]))
            switch.assert_called_with("s1", False)
            switch.side_effect = ValueError("The Codex module is off")
            code, _, body = post("/api/sessions/s1/switch-agent", {"confirm_hidden": True}, token=t)
            self.assertEqual(code, 409)   # a plain refusal, no hidden_paths
            self.assertIn("Codex module is off", json.loads(body)["error"])
            self.assertNotIn("hidden_paths", json.loads(body))
            switch.assert_called_with("s1", True)

    def test_shell_csp_nonce(self):
        code, h, body = get("/")
        self.assertEqual(code, 200)
        m = re.search(r"'nonce-([^']+)'", h["content-security-policy"])
        self.assertIsNotNone(m)
        self.assertIn(('<script nonce="%s">window.__PIPER__=' % m.group(1)).encode(), body)
        self.assertIn(('<script type="importmap" nonce="%s">' % m.group(1)).encode(), body)
        self.assertIn("style-src 'self' 'unsafe-inline'", h["content-security-policy"])
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertNotIn(self.tok.encode(), body)

    def test_mattermost_presence_and_input_clear_routes(self):
        t, m = self.tok, piperd.MANAGER
        self.assertEqual(post("/api/presence", {"visible": True})[0], 401)
        self.assertEqual(post("/api/presence", {"visible": True}, token=t)[0], 200)
        self.assertEqual(post("/api/presence", {"visible": "yes"}, token=t)[0], 200)
        self.assertEqual(m.created[-2:], [("presence", True), ("presence", False)])   # only a real true
        code, _, body = get("/api/mattermost", token=t)
        self.assertEqual((code, json.loads(body)["mode"]), (200, "webhook"))
        self.assertEqual(post("/api/mattermost/test", {}, token=t)[0], 409)
        self.assertEqual(post("/api/sessions/s1/input-clear", {}, token=t)[0], 200)
        self.assertEqual(m.created[-1], ("input-clear", "s1"))
        self.assertEqual(post("/api/sessions/zz/input-clear", {}, token=t)[0], 404)

    def test_other_methods_and_dropped_connections(self):
        """review 2026-10-05: PUT/DELETE got a 501 HTML page; a reset idle keep-alive connection
        (a reload) printed a traceback"""
        code, _, body = request(("DELETE /api/state HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % HOST).encode())
        self.assertEqual((code, json.loads(body)), (405, {"error": "method not allowed"}))
        from http.server import BaseHTTPRequestHandler
        from unittest import mock
        a, b = socket.socketpair()
        self.addCleanup(b.close)
        with mock.patch.object(BaseHTTPRequestHandler, "handle", side_effect=ConnectionResetError(54, "reset")):
            serve(a)   # raised before: socketserver printed it as a traceback
        self.assertEqual(get("/")[0], 200)

    def test_git_of_a_session_without_a_working_tree_is_a_json_409(self):
        """F-N10-7 (GUI pass D, N10-D-7): GET /api/sessions/<prodcheck|advisor>/git - _turn_top raises ValueError,
        the route caught only KeyError / GitError: the GUI got an empty reply (FETCH-FAILED), piperd a traceback."""
        t = common.own_token("gui")
        why = "only a project session has a working tree"
        with mock.patch.object(piperd.MANAGER, "git_status", create=True, side_effect=ValueError(why)), \
                mock.patch.object(piperd.MANAGER, "git_diff", create=True, side_effect=ValueError(why)):
            for path in ("/api/sessions/s1/git", "/api/sessions/s1/git?diff=a.txt"):
                code, _, body = get(path, token=t)
                self.assertEqual((code, json.loads(body)), (409, {"error": why}), path)

    def test_an_unexpected_exception_in_any_route_is_a_json_500_logged_not_sent(self):
        """F-N10-7 (general): an exception no route maps (a bug) left the connection without an answer - the
        client saw an empty reply. Now one JSON 500 for GET and POST, nothing of the exception in the body;
        the traceback (with the route) goes to piperd.log (stderr), never the request headers (the token)."""
        import contextlib
        import io
        t = common.own_token("gui")
        boom = RuntimeError("PLANTED_SECRET boom")
        for method, path, patch in (("GET", "/api/sessions/s1/draft", "session_draft"), ("POST", "/api/sessions/s1/send", "send")):
            err = io.StringIO()
            with mock.patch.object(piperd.MANAGER, patch, create=True, side_effect=boom), contextlib.redirect_stderr(err):
                code, h, body = get(path, token=t) if method == "GET" else post(path, {"text": "x"}, token=t)
            self.assertEqual(code, 500, path)
            self.assertEqual(h["content-type"], "application/json")
            self.assertEqual(json.loads(body), {"error": "internal error - see piperd.log"})
            self.assertNotIn(b"PLANTED_SECRET", body)
            self.assertNotIn(b"Traceback", body)
            log = err.getvalue()
            self.assertIn("PLANTED_SECRET boom", log)
            self.assertIn("Traceback", log)
            self.assertIn("%s %s" % (method, path), log)
            self.assertNotIn(t, log)
        self.assertEqual(get("/api/state", token=t)[0], 200)   # the next request is answered as before

    def test_the_tty_token_cannot_change_settings(self):
        """review 2026-10-05: `piper attach`'s token (label tty) had the GUI's full scope"""
        tty = common.mint_token("tty")
        code, _, body = post("/api/settings", {"section": "terminal", "values": {}}, token=tty)
        self.assertEqual(code, 403)
        self.assertIn("GUI token", json.loads(body)["error"])
        self.assertEqual(get("/api/state", token=tty)[0], 200)   # terminals and the list stay

    def test_the_tty_token_reads_neither_the_board_nor_the_production_report(self):
        """N3 review (2026-10-09): /api/state gives `piper attach` no prod (L1) and /conversation is CONTENT_SCOPE
        (S6) - but GET /api/board (the last 700 characters of each session's answer, the last prompt) and GET
        /api/prod (the production log report) answered the tty token in full."""
        tty = common.mint_token("tty")
        board = [{"id": "s1", "last": "PLANTED_BOARD answer", "asked": "x", "changed": 0}]
        with mock.patch.object(piperd.MANAGER, "board", lambda: board, create=True):
            for path in ("/api/board", "/api/prod"):
                code, _, body = get(path, token=tty)
                self.assertEqual(code, 403, path)
                self.assertNotIn(b"PLANTED_BOARD", body)
            self.assertEqual(json.loads(get("/api/board", token=self.tok)[2])["cards"], board)   # the GUI keeps both
            self.assertEqual(get("/api/prod", token=self.tok)[0], 200)

    def test_the_tty_token_state_carries_no_prompt_task_or_issue_of_a_session(self):
        """N4 review: /api/board was gated for its last prompt (N3 D1), but /api/state handed `piper attach`
        every session's task, first / last prompt, GitLab issue, edited paths and agent bookkeeping. The tty
        list keeps what ttyattach.py reads (id, kind, project, title, state, detail, blocks, pid, rc, notice...)."""
        tty = common.mint_token("tty")
        row = {"id": "s1", "kind": "project", "project": "shop", "path": "/p/shop", "title": "shop", "state": "ready",
               "phase": "", "detail": "", "blocks": [], "pid": 7, "rc": None, "notice": "", "claude_session": "c1",
               "agent": "claude", "alive": True, "tty": False, "origin": "gui", "created": 1.0, "since": 1.0,
               "task": "PLANTED_TASK", "first_prompt": "PLANTED_FIRST", "last_prompt": "PLANTED_LAST",
               "issue": {"title": "PLANTED_ISSUE"}, "fsedit": {"seq": 1, "paths": ["PLANTED_PATH"]},
               "subagents": {"a": "PLANTED_SUB"}, "background_tasks": {"t": {"label": "PLANTED_BG"}}}
        with mock.patch.object(piperd.MANAGER, "list", lambda: [dict(row)]):
            code, _, body = get("/api/state", token=tty)
            self.assertEqual(code, 200)
            self.assertNotIn(b"PLANTED_", body)
            s = json.loads(body)["sessions"][0]
            for k in ("id", "kind", "project", "title", "state", "detail", "blocks", "pid", "rc", "notice",
                      "claude_session", "agent", "alive", "tty"):
                self.assertIn(k, s)
            self.assertIn(b"PLANTED_LAST", get("/api/state", token=self.tok)[2])   # the GUI keeps them

    def test_the_tty_token_reads_neither_the_audit_log_nor_the_timesheet(self):
        """N4 review: N3 listed /api/audit and /api/timesheet as content-free - the audit log holds the agent's
        git_run argv (commit messages, grep patterns), spawn argv and session titles; the timesheet's notes
        are the prompts. `piper attach` reads neither."""
        tty = common.mint_token("tty")
        audit = [{"event": "git-run", "session": "s1", "args": ["commit", "-m", "PLANTED_MSG"]}]
        sheet = {"days": [{"notes": ["PLANTED_PROMPT"]}]}
        with mock.patch.object(piperd.MANAGER, "audit_log", lambda sid, limit: audit, create=True), \
                mock.patch.object(piperd.MANAGER, "timesheet", lambda week: sheet, create=True):
            for path in ("/api/audit", "/api/timesheet"):
                code, _, body = get(path, token=tty)
                self.assertEqual(code, 403, path)
                self.assertNotIn(b"PLANTED_", body)
                self.assertEqual(get(path, token=self.tok)[0], 200, path)   # the GUI keeps both

    # every POST route piperd has (token-scope audit 2026-10-07); only the two terminal ones are open to
    # `piper attach`'s token and a scoped one - and an unknown route is refused before any dispatch,
    # so a route added later is the GUI's by default
    POST_ROUTES = (
        ["settings"], ["settings", "verify-gitlab"], ["features"], ["fs", "write"], ["fs", "create"],
        ["fs", "rename"], ["fs", "delete"], ["fs", "restore"], ["git", "mr"], ["open-url"], ["presence"],
        ["mattermost", "test"], ["stack", "restart"], ["stack", "action"], ["stack", "cancel"],
        ["projects", "add"], ["projects", "import"], ["pending", "p1"],
        *[["http", op] for op in ("run", "envcfg", "tunnel", "share", "install")],
        *[["db", op] for op in ("query", "fetch", "execute", "cancel", "tx", "save", "delete", "unpin", "tunnel", "install")],
        *[["prod", op] for op in ("run", "mapping", "ignore", "mute", "unmute", "investigate")],
        *[["feed", op] for op in ("seen", "dismiss", "undismiss", "mute", "chat")],
        *[["feed", "chat", op] for op in ("cancel", "retry", "start")],
        *[["inbox", op] for op in ("refresh", "seen", "unseen", "done", "start", "comment", "rules")],
        *[["timesheet", op] for op in ("focus", "edit", "manual", "assign")],
        *[["sessions", "s1", op] for op in ("fix-excludes", "allow-uncovered", "open-terminal", "send", "title", "input-clear", "allow",
                                            "unlock", "start-opt", "issue-state", "stop", "approve", "forget", "drop")],
        ["sessions", "s1", "git", "commit"], ["sessions", "s1", "git", "push"], ["no", "such", "route"])

    def test_allow_uncovered_dispatches_only_with_gui_token(self):
        with mock.patch.object(piperd.MANAGER, "allow_uncovered", return_value={"allowed": ["/project (+1)"]}, create=True) as allow:
            code, _, body = post("/api/sessions/s1/allow-uncovered", {}, self.tok)
            self.assertEqual(code, 200, body)
            self.assertEqual(json.loads(body), {"allowed": ["/project (+1)"]})
            allow.assert_called_once_with("s1")

    def test_only_the_gui_token_changes_anything(self):
        with open(common.TOKENS_FILE, "a") as f:
            f.write("scoped-any\tsession:s1\thook\n")
        n = len(piperd.MANAGER.created)
        for route in self.POST_ROUTES:
            self.assertFalse(piperd.terminal_post(["api"] + route), route)
            for tok in (common.own_token("tty"), "scoped-any"):
                code, _, body = post("/api/" + "/".join(route), {"project": "team-x/app", "files": []}, tok)
                self.assertEqual(code, 403, (route, body))
        self.assertEqual(len(piperd.MANAGER.created), n)
        self.assertEqual(post("/api/no/such/route", {}, self.tok)[0], 404)   # the GUI token still reaches the router
        for route in (["sessions"], ["sessions", "s1", "retry"]):   # what `piper attach` does
            self.assertTrue(piperd.terminal_post(["api"] + route))
        self.assertEqual(post("/api/sessions", {"project": "x", "origin": "tty"}, common.own_token("tty"))[0], 202)
        self.assertEqual(post("/api/sessions/s1/retry", {}, common.own_token("tty"))[0], 200)

    def test_host_header(self):
        self.assertEqual(get("/", host="evil.example:%d" % PORT)[0], 421)
        self.assertEqual(get("/", host="localhost:%d" % PORT)[0], 200)

    def test_remote_hosts_through_tailscale_serve(self):
        """9.56: PIPER_ALLOWED_HOSTS names are a Host only over https, exact names only, fail-closed."""
        ts = "mac.tail-example.ts.net"
        https = "Origin: https://%s\r\n" % ts
        tok = common.own_token("gui")
        self.assertEqual(get("/", host=ts)[0], 421)   # unset: today's behaviour
        self.assertEqual(get("/api/state", host="127.0.0.1:%d" % PORT, token=tok, extra=https)[0], 403)
        with mock.patch.dict(os.environ, {"PIPER_ALLOWED_HOSTS": " %s ,other.ts.net" % ts.upper()}):
            code, _, body = get("/", host=ts)
            self.assertEqual(code, 200)
            self.assertIn(b'"dashboardUrl": ""', body)   # a provider's loopback link means nothing remotely
            self.assertEqual(get("/", host=ts + ":443")[0], 200)
            self.assertIn(b"127.0.0.1:", get("/")[2])     # loopback keeps the link
            self.assertEqual(get("/api/state", host=ts, token=tok, extra=https)[0], 200)
            self.assertEqual(get("/api/state", host=ts, extra=https)[0], 401)   # never instead of the token
            self.assertEqual(get("/api/state", host=ts, token=tok, extra="Origin: http://%s\r\n" % ts)[0], 403)
            self.assertEqual(get("/api/state", host=ts, token=tok, extra="Origin: https://evil.ts.net\r\n")[0], 403)
            self.assertEqual(get("/api/state", host=ts, token=tok, extra="Origin: http://127.0.0.1:%d\r\n" % PORT)[0], 403)
            for evil in ("evil.ts.net", ts + ".evil.com", "x." + ts, ts + ":8443"):
                self.assertEqual(get("/", host=evil)[0], 421, evil)
            # serve rewriting Host to loopback: X-Forwarded-Host still marks the request remote
            fwd = "X-Forwarded-Host: %s\r\n" % ts
            self.assertEqual(get("/api/state", token=tok, extra=fwd + https)[0], 200)
            self.assertEqual(get("/api/state", token=tok, extra=fwd + "Origin: http://%s\r\n" % ts)[0], 403)
            self.assertIn(b'"dashboardUrl": ""', get("/", extra=fwd)[2])
            # identity: Tailscale-User-Login on top of the token
            with mock.patch.dict(os.environ, {"PIPER_ALLOWED_TS_USERS": "owner@example.com"}):
                me, other = "Tailscale-User-Login: Owner@example.com\r\n", "Tailscale-User-Login: x@example.com\r\n"
                self.assertEqual(get("/api/state", host=ts, token=tok, extra=https + me)[0], 200)
                self.assertEqual(get("/api/state", host=ts, token=tok, extra=https + other)[0], 403)
                self.assertEqual(get("/api/state", host=ts, token=tok, extra=https)[0], 403)   # a tagged node
                self.assertEqual(get("/api/state", token=tok)[0], 200)   # local requests carry no identity
            raw = json.dumps({}).encode()
            hdr = ("POST /api/no/such/route HTTP/1.1\r\nHost: %s\r\nOrigin: http://%s\r\nAuthorization: Bearer %s\r\n"
                   "Content-Length: %d\r\nConnection: close\r\n\r\n" % (ts, ts, tok, len(raw)))
            self.assertEqual(request(hdr.encode() + raw)[0], 403)
            self.assertEqual(request(hdr.replace("http://", "https://").encode() + raw)[0], 404)
        # Tailscale headers without a configured name: nothing remote
        self.assertEqual(get("/api/state", token=tok, extra="Tailscale-User-Login: owner@example.com\r\n")[0], 403)

    def test_remote_names_are_exact(self):
        for bad in ("*.ts.net", "mac.ts.net:443", "https://mac.ts.net", "mac.ts.net/x", "100.64.0.1",
                    "localhost", "a.localhost", "mac", ".ts.net", "a b.ts.net"):
            with mock.patch.dict(os.environ, {"PIPER_ALLOWED_HOSTS": bad}):
                self.assertIsNotNone(piperd.check_remote_names(), bad)
        with mock.patch.dict(os.environ, {"PIPER_ALLOWED_HOSTS": "mac.tail-example.ts.net,b.ts.net"}):
            self.assertIsNone(piperd.check_remote_names())
        with mock.patch.dict(os.environ, {"PIPER_ALLOWED_HOSTS": ""}):
            self.assertIsNone(piperd.check_remote_names())

    def test_remote_url_has_its_own_revocable_token(self):
        with mock.patch.dict(os.environ, {"PIPER_ALLOWED_HOSTS": ""}):
            with self.assertRaises(SystemExit):
                piperd.gui_url(remote=True)
        with mock.patch.dict(os.environ, {"PIPER_ALLOWED_HOSTS": "mac.tail-example.ts.net,b.ts.net"}):
            url = piperd.gui_url(remote=True)
            self.assertTrue(url.startswith("https://mac.tail-example.ts.net/#token="), url)
            rtok = url.split("#token=", 1)[1]
            self.assertNotEqual(rtok, common.own_token("gui"))
            self.assertEqual(common.load_tokens()[rtok]["label"], "remote")
            self.assertEqual(piperd.gui_url(remote=True), url)   # stable until revoked
            https = "Origin: https://mac.tail-example.ts.net\r\n"
            self.assertEqual(get("/api/settings", host="mac.tail-example.ts.net", token=rtok, extra=https)[0], 200)
            gui = common.own_token("gui")
            self.assertEqual(common.revoke_token("remote"), 1)
            self.assertEqual(get("/api/state", host="mac.tail-example.ts.net", token=rtok, extra=https)[0], 401)
            self.assertIn(gui, common.load_tokens())   # the host's GUI token stays
            self.assertNotIn(rtok, piperd.gui_url(remote=True))
        self.assertTrue(piperd.gui_url().startswith("http://127.0.0.1:%d/#token=" % PORT))

    def test_listen_backlog_takes_a_burst(self):
        """9.56: behind `tailscale serve` the GUI's module burst opens ~60 connections at once; a
        backlog of 5 reset most of them (502 in the browser). A real listener, 100 simultaneous clients."""
        srv = piperd.Server(("127.0.0.1", 0), piperd.Handler)
        self.assertGreaterEqual(srv.request_queue_size, 128)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port, go, ok, lock = srv.server_address[1], threading.Event(), [], threading.Lock()

        def one():
            go.wait()
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
                    s.sendall(("GET /piper/app.css HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % HOST).encode())
                    data = b""
                    while True:
                        c = s.recv(65536)
                        if not c:
                            break
                        data += c
                    if data.startswith(b"HTTP/1.1 200"):
                        with lock:
                            ok.append(1)
            except OSError:
                pass
        ts = [threading.Thread(target=one) for _ in range(100)]
        for t in ts:
            t.start()
        go.set()
        for t in ts:
            t.join()
        self.assertGreaterEqual(len(ok), 95)   # backlog 5: about 10 got through

    def test_api_needs_token(self):
        self.assertEqual(get("/api/state")[0], 401)
        self.assertEqual(get("/api/state", token="wrong")[0], 401)
        code, h, body = get("/api/state", token=self.tok)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["sessions"][0]["id"], "s1")
        self.assertEqual(get("/api/state", token=self.tok, extra="If-None-Match: %s\r\n" % h["etag"])[0], 304)

    def test_pending_session_grant_requires_gui_and_passes_only_a_real_boolean(self):
        with mock.patch.object(piperd.MANAGER, "decide", create=True) as decide:
            for flag in (True, False, "true"):
                self.assertEqual(post("/api/pending/p1", {"approve": True, "session_always": flag}, token=self.tok)[0], 200)
                decide.assert_called_with("p1", True, False, **({"session_always": True} if flag is True else {}))
            decide.reset_mock()
            tty = common.own_token("tty")
            self.assertEqual(post("/api/pending/p1", {"approve": True, "session_always": True}, token=tty)[0], 403)
            decide.assert_not_called()

    def test_state_says_the_theme(self):
        # N1: Settings › Interface › Theme reaches the GUI on the next poll; dark without a policy
        m = piperd.MANAGER
        old = getattr(m, "policy", None)
        try:
            for policy, want in ((None, "dark"), ({}, "dark"), ({"theme": "light"}, "light"), ({"theme": "indigo"}, "indigo")):
                m.policy = policy
                self.assertEqual(json.loads(get("/api/state", token=self.tok)[2])["gui_flags"]["theme"], want, policy)
        finally:
            m.policy = old

    def test_state_says_the_git_mode(self):
        # the inspector must not show "Full git: locked" when git_mode "hole" runs git_run without an unlock (E11)
        m = piperd.MANAGER
        old = getattr(m, "policy", None)
        try:
            for policy, want in ((None, True), ({}, True), ({"git_mode": "hole"}, True), ({"git_mode": "guarded"}, False)):
                m.policy = policy
                self.assertIs(json.loads(get("/api/state", token=self.tok)[2])["gui_flags"]["git_hole"], want, policy)
            self.assertEqual(json.loads(get("/api/state", token=self.tok)[2])["pending_gone"], [], "F17: the gone rows ride along")
        finally:
            m.policy = old

    def test_state_exposes_effective_security_controls(self):
        m = piperd.MANAGER
        old = getattr(m, "policy", None)
        try:
            m.policy = {"unlocks_required": False, "attestation": False, "l5_action": "off"}
            flags = json.loads(get("/api/state", token=self.tok)[2])["gui_flags"]
            self.assertFalse(flags["commit_requires_review"])   # commit A: Review is optional unless Settings → Git asks
            self.assertFalse(flags["unlocks_required"])
            self.assertFalse(flags["attestation"])
            self.assertFalse(flags["l5_watch"])
        finally:
            m.policy = old

    def test_stack_off_does_not_probe_the_provider(self):
        from unittest import mock
        with mock.patch.object(piperd.features, "on", return_value=False), \
                mock.patch.object(piperd.MANAGER, "_stack_up", return_value=True, create=True) as probe:
            code, _, body = get("/api/state", token=self.tok)
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)["stack_up"])
        probe.assert_not_called()
        with mock.patch.object(piperd.features, "on", return_value=True), \
                mock.patch.object(piperd.MANAGER, "_stack_up", return_value=True, create=True):   # an extension's module on
            self.assertTrue(json.loads(get("/api/state", token=self.tok)[2])["stack_up"])

    def test_codex_limits_state_requires_codex_and_usage_features(self):
        original = piperd.features.on
        for usage, codex in ((True, True), (True, False), (False, True), (False, False)):
            switches = {"usage": usage, "codex": codex}
            with mock.patch.object(piperd.features, "on", side_effect=lambda key: switches.get(key, original(key))), \
                    mock.patch.object(piperd.codexlimits, "summary", return_value={"status": "ok"}) as summary:
                code, _, body = get("/api/state", token=self.tok)
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body)["codex_limits"], {"status": "ok"} if usage and codex else None)
                self.assertEqual(summary.call_count, int(usage and codex))

    def test_prod_routes(self):
        t = self.tok
        st = json.loads(get("/api/state", token=t)[2])
        self.assertEqual(st["prod"]["status"], "idle")
        self.assertEqual(st["queue"], {"actions": []})   # the Overview queue (9.16), also with the Feed off
        self.assertEqual(get("/api/prod")[0], 401)
        self.assertEqual(json.loads(get("/api/prod", token=t)[2]), {"status": "idle", "report": None})
        code, _, body = post("/api/prod/run", {}, token=t)
        self.assertEqual((code, json.loads(body)["session"]), (202, "p1"))
        self.assertEqual(post("/api/prod/run", {"mode": "x"}, token=t)[0], 409)
        self.assertEqual(post("/api/prod/nonsense", {}, token=t)[0], 404)

    def test_workbench_routes(self):
        t = self.tok
        self.assertEqual(get("/api/fs/list?project=team-x/app&path=src")[0], 401)
        code, _, body = get("/api/fs/read?project=team-x%2Fapp&path=src%2FA.java", token=t)
        self.assertEqual((code, json.loads(body)), (200, {"op": "read"}))
        self.assertIn(("fs", "team-x/app", "read", "src/A.java"), piperd.MANAGER.created)
        self.assertEqual(get("/api/fs/read?project=team-x%2Fapp&path=secret", token=t)[0], 404)
        # file content (a file hidden from Claude included, 2026-10-07): the full GUI token only, never `piper attach`'s
        tty, pj = common.mint_token("tty"), "project=team-x%2Fapp"
        for path in ("/api/fs/read?project=team-x%2Fapp&path=src%2FA.java", "/api/fs/list?project=team-x%2Fapp&path=src",
                     "/api/fs/grep?project=team-x%2Fapp&q=x", "/api/fs/marks?%s&path=a" % pj, "/api/git/show?%s&commit=abcd" % pj,
                     "/api/git/diff?%s&path=a" % pj, "/api/idea/problems?%s&path=a" % pj, "/api/sessions/s1/git",
                     "/api/sessions/s1/git?diff=a", "/api/sessions/s1/turn?from=t1&to=t2&diff=.", "/api/sessions/s1/turns",
                     "/api/git/status?%s" % pj,
                     # S6: what holds file content too - `piper attach` uses /api/state, POST /api/sessions, /ws only
                     "/api/sessions/s1/conversation", "/api/conversations/search?q=abc", "/api/sessions/s1/diagnostics",
                     "/api/stack/logs?%s&service=app" % pj, "/api/stack/job?%s&id=abcdef123456" % pj,
                     "/api/http/runs?%s&path=a.http" % pj, "/api/http/run?id=x",
                     # review 2026-10-08: raw inbox / GitLab text (L1), the Feed's card bodies, a comment draft
                     "/api/inbox", "/api/inbox/item?key=x", "/api/feed", "/api/feed/chat", "/api/sessions/s1/draft",
                     "/api/gitlab/issues?%s" % pj, "/api/gitlab/issue?%s&iid=1" % pj,
                     "/api/http/envs?%s&path=a.http" % pj, "/api/http/files?%s" % pj):
            self.assertEqual(get(path, token=tty)[0], 403, path)
        self.assertEqual(get("/api/idea/status", token=tty)[0], 200)
        self.assertEqual(get("/api/state", token=tty)[0], 200)
        card = {"id": "c1", "session": "s1", "title": "app", "kind": "db_write", "summary": "UPDATE ...",
                "payload": {"card": {"sample": [["jan@example.com"]]}}}
        with mock.patch.object(piperd.MANAGER, "pending_list", lambda: [card], create=True):
            pend = json.loads(get("/api/state", token=tty)[2])["pending"]
            self.assertEqual(pend, [{"id": "c1", "session": "s1", "title": "app", "kind": "db_write"}])   # attach rings only
            self.assertIn("payload", json.loads(get("/api/state", token=t)[2])["pending"][0])
        st = json.loads(get("/api/state", token=tty)[2])   # no GitLab text / Feed for attach (L1: raw since 2026-10-08)
        self.assertEqual([st[k] for k in ("inbox", "prod", "feed", "queue")], [{}, {}, {}, {}])
        code, _, body = get("/api/fs/list?project=team-x%2Fnope", token=t)
        self.assertEqual((code, json.loads(body)["error"]), (404, "unknown project 'team-x/nope'"))   # not "unknown: unknown"
        code, _, body = get("/api/fs/read?project=team-x%2Fapp&path=a&offset=abc", token=t)
        self.assertEqual((code, json.loads(body)["error"]), (400, "invalid offset"))
        # K13: a file too large or binary exists - 413 / 415, not 404
        self.assertEqual(get("/api/fs/read?project=team-x%2Fapp&path=big.log", token=t)[0], 413)
        self.assertEqual(get("/api/fs/read?project=team-x%2Fapp&path=a.png", token=t)[0], 415)
        # K2: the git status of a project without a session
        code, _, body = get("/api/git/status?project=team-x%2Fapp", token=t)
        self.assertEqual((code, json.loads(body)["branch"]), (200, "main"))
        self.assertEqual(get("/api/git/status?project=team-x%2Fnope", token=t)[0], 404)
        self.assertEqual(get("/api/fs/read?project=team-x%2Fapp&path=a&limit=1.5", token=t)[0], 400)
        self.assertEqual(get("/api/gitlab/issues?project=team-x%2Fapp", token=t)[0], 503)
        self.assertEqual(json.loads(get("/api/gitlab/issue?project=team-x%2Fapp&iid=7", token=t)[2]), {"iid": 7})
        code, _, body = get("/api/gitlab/issue?project=team-x%2Fapp&iid=x", token=t)
        self.assertEqual((code, json.loads(body)["error"]), (400, "invalid iid"))
        self.assertEqual(get("/api/audit?limit=x", token=t)[0], 400)
        self.assertEqual(get("/api/sessions/s1/conversation", token=t)[0], 200)
        self.assertEqual(get("/api/sessions/zz/conversation", token=t)[0], 404)
        self.assertEqual(get("/api/conversations/search?q=a", token=t)[0], 409)
        self.assertEqual(json.loads(get("/api/board", token=t)[2])["cards"][0]["id"], "s1")
        ev = json.loads(get("/api/audit?session=s1&limit=5", token=t)[2])["events"]
        self.assertEqual(ev, [{"event": "x", "session": "s1", "limit": 5}])
        self.assertEqual(post("/api/sessions/s1/send", {"text": "hi"})[0], 401)
        self.assertEqual(post("/api/sessions/s1/send", {"text": "hi"}, token=t)[0], 200)
        self.assertIn(("send", "s1", "hi"), piperd.MANAGER.created)
        self.assertEqual(post("/api/sessions/s1/send", {"text": ""}, token=t)[0], 409)
        self.assertEqual(post("/api/sessions/zz/send", {"text": "hi"}, token=t)[0], 404)
        self.assertEqual(post("/api/sessions/s1/allow", {"domain": "gitlab.com"}, token=t)[0], 409)
        self.assertEqual(post("/api/sessions/s1/allow", {"domain": "x.example"}, token=t)[0], 200)
        self.assertEqual(post("/api/sessions/s1/send", {"text": "hi"}, token=t, origin="http://evil.example")[0], 403)

    def test_blob_requires_gui_and_opt_in(self):
        url = "/api/fs/blob?project=team-x/app&path=src/A.java&tree=HEAD"
        self.assertEqual(get(url)[0], 401)
        self.assertEqual(get(url, token=common.mint_token("tty"))[0], 403)
        from unittest import mock
        with mock.patch.object(piperd.MANAGER, "fs_blob", return_value={"state": "text", "text": "ok"}, create=True) as blob:
            code, _, body = get(url, token=self.tok)
            self.assertEqual((code, json.loads(body)["text"]), (200, "ok"))
            blob.assert_called_once_with("team-x/app", "src/A.java", "HEAD", "")

    def test_review_routes_and_commits_require_gui_and_opt_in(self):
        from unittest import mock
        routes = [("/review?scope=turn&n=2", "review_files", ("s1", "turn", 2)),
                  ("/review/blob?review=r&path=a&side=left", "review_blob", ("s1", "r", "a", "left")),
                  ("/review/check?review=r", "review_check", ("s1", "r", "")),
                  ("/review/check?review=r&path=a", "review_check", ("s1", "r", "a")),
                  ("/review/history?review=r&path=a", "review_history", ("s1", "r", "a"))]
        tty = common.mint_token("tty")
        for suffix, method, args in routes:
            url = "/api/sessions/s1" + suffix
            self.assertEqual(get(url)[0], 401)
            self.assertEqual(get(url, token=tty)[0], 403)
            with mock.patch.object(piperd.MANAGER, method, return_value={"ok": True}, create=True) as call:
                self.assertEqual(get(url, token=self.tok)[0], 200)
                call.assert_called_once_with(*args)
        body = {"message": "reviewed", "files": ["a"], "reviewed": {"review": "r"}}
        with mock.patch.object(piperd.MANAGER, "git_commit", return_value={"commit": "abc"}) as call:
            self.assertEqual(post("/api/sessions/s1/git/commit", body, token=tty)[0], 403)
            call.assert_not_called()
            self.assertEqual(post("/api/sessions/s1/git/commit", body, token=self.tok)[0], 200)
            call.assert_called_once_with("s1", "reviewed", ["a"], body["reviewed"])

    def test_review_action_routes_require_gui_and_opt_in(self):
        from unittest import mock
        import reviewactions
        tty = common.mint_token("tty")
        body = {"review": "r", "path": "a", "text": None, "sha": "version"}
        routes = [("stage", "review_stage", ("s1", "r", "a"), 200),
                  ("write", "review_write", ("s1", "r", "a", None, "version"), 200),
                  ("dispatch", "review_dispatch", ("s1", body), 202)]
        for suffix, method, args, success in routes:
            url = "/api/sessions/s1/review/" + suffix
            self.assertEqual(post(url, body)[0], 401)
            with mock.patch.object(piperd.MANAGER, method, return_value={"ok": True}, create=True) as call:
                self.assertEqual(post(url, body, token=tty)[0], 403)
                call.assert_not_called()
                self.assertEqual(post(url, body, token=self.tok)[0], success)
                call.assert_called_once_with(*args)
        url = "/api/sessions/s1/review/config"
        with mock.patch.object(reviewactions, "load", return_value=[{"id": "custom"}]) as config:
            self.assertEqual(get(url, token=tty)[0], 403)
            config.assert_not_called()
            code, _, payload = get(url, token=self.tok)
            self.assertEqual((code, json.loads(payload)), (200, {"actions": [{"id": "custom"}]}))

    def test_workbench97_routes(self):
        t, pj = self.tok, "project=team-x%2Fapp"
        code, _, body = get("/api/fs/marks?%s&path=src/A.java" % pj, token=t)
        self.assertEqual((code, json.loads(body)["added"]), (200, [[1, 2]]))
        self.assertEqual(get("/api/fs/marks?%s&path=secret.txt" % pj, token=t)[0], 404)
        self.assertEqual(get("/api/fs/marks?%s&path=src/A.java" % pj)[0], 401)
        self.assertEqual(json.loads(get("/api/fs/symbols?%s&name=Foo" % pj, token=t)[2])["symbols"], [{"name": "Foo"}])
        self.assertEqual(get("/api/fs/symbols?%s" % pj, token=t)[0], 409)
        code, h, body = get("/api/fs/raw?%s&path=img/a.png" % pj, token=t)
        self.assertEqual((code, h["content-type"], body[:4]), (200, "image/png", b"\x89PNG"))
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", h["content-security-policy"])
        self.assertEqual(get("/api/fs/raw?%s&path=a.txt" % pj, token=t)[0], 409)
        self.assertEqual(get("/api/fs/raw?%s&path=secret.png" % pj, token=t)[0], 404)
        self.assertEqual(get("/api/fs/read?%s&path=src" % pj, token=t)[0], 200)        # the generic fs routes stay
        code, h, body = get("/api/git/diff?%s&path=src/A.java" % pj, token=t)
        self.assertEqual((code, h["content-type"], body), (200, "text/plain; charset=utf-8", b"diff --git a/src/A.java b/src/A.java\n"))
        self.assertEqual(get("/api/git/diff?%s&path=secret" % pj, token=t)[0], 404)
        self.assertEqual(json.loads(get("/api/git/log?%s&n=7" % pj, token=t)[2])["commits"][0]["n"], 7)
        self.assertEqual(get("/api/git/log?%s&n=x" % pj, token=t)[0], 400)
        self.assertEqual(get("/api/stack/logs?%s&service=db&tail=x" % pj, token=t)[0], 400)
        self.assertEqual(get("/api/idea/symbol?%s&path=a&line=3&col=c" % pj, token=t)[0], 400)
        self.assertEqual(get("/api/git/blame?%s&path=secret" % pj, token=t)[0], 404)
        self.assertEqual(json.loads(get("/api/git/graph?%s&n=3" % pj, token=t)[2])["lines"], ["* x 3"])
        self.assertEqual(get("/api/git/show?%s&commit=abcd" % pj, token=t)[2], b"commit abcd\n")
        self.assertEqual(get("/api/git/show?%s&commit=0000" % pj, token=t)[0], 409)
        self.assertEqual(json.loads(get("/api/git/ci?%s" % pj, token=t)[2])["branch"], "main")
        self.assertEqual(json.loads(get("/api/git/mr-draft?%s&target=dev" % pj, token=t)[2])["target"], "dev")
        self.assertEqual(get("/api/git/nope?%s" % pj, token=t)[0], 404)
        self.assertIsNone(json.loads(get("/api/stack?%s" % pj, token=t)[2])["stack"])
        self.assertEqual(get("/api/stack/logs?%s&service=db&tail=5" % pj, token=t)[2], b"log db 5\n")
        self.assertEqual(json.loads(get("/api/stack/job?%s&id=abcdef123456&offset=4" % pj, token=t)[2])["offset"], 7)
        self.assertEqual(get("/api/stack/job?%s&id=ffffff000000" % pj, token=t)[0], 404)
        self.assertEqual(get("/api/stack/job?%s&id=abcdef123456&offset=x" % pj, token=t)[0], 400)
        self.assertEqual(json.loads(get("/api/sessions/s1/turns", token=t)[2])["turns"], [{"n": 1}])
        self.assertEqual(get("/api/sessions/zz/turns", token=t)[0], 404)
        self.assertEqual(json.loads(get("/api/sessions/s1/turn?from=t1", token=t)[2])["to"], "now")
        self.assertEqual(get("/api/sessions/s1/turn?from=bad", token=t)[0], 409)
        self.assertEqual(get("/api/sessions/s1/turn?from=t1&to=t2&diff=.", token=t)[2], b"turn diff .\n")
        self.assertEqual(get("/api/sessions/s1/diagnostics", token=t)[0], 200)
        self.assertFalse(json.loads(get("/api/idea/status", token=t)[2])["available"])
        self.assertEqual(get("/api/idea/problems?%s&path=secret" % pj, token=t)[0], 404)
        self.assertEqual(json.loads(get("/api/idea/symbol?%s&path=a&line=3&col=4" % pj, token=t)[2])["info"], "3:4")
        self.assertEqual(json.loads(get("/api/usage", token=t)[2])["total"]["input"], 0)
        self.assertIn("img-src 'self' data: blob:", get("/")[1]["content-security-policy"])

    def test_workbench97_posts(self):
        t = self.tok
        self.assertEqual(post("/api/fs/write", {"project": "team-x/app", "path": "a", "text": "x\n", "sha": "ok"})[0], 401)
        code, _, body = post("/api/fs/write", {"project": "team-x/app", "path": "a", "text": "x\n", "sha": "ok"}, t)
        self.assertEqual((code, json.loads(body)["sha"]), (200, "new"))
        self.assertEqual(post("/api/fs/write", {"project": "team-x/app", "path": "a", "text": "x", "sha": "old"}, t)[0], 409)
        self.assertEqual(post("/api/fs/write", {"project": "team-x/app", "path": "secret", "text": "x", "sha": "ok"}, t)[0], 404)
        self.assertEqual(post("/api/fs/write", {"project": "team-x/nope", "path": "a", "text": "x", "sha": "ok"}, t)[0], 404)
        self.assertEqual(post("/api/fs/write", {"project": "team-x/app", "path": "a", "text": "x\n", "sha": "ok"},
                              common.mint_token("tty"))[0], 403)
        big = "y" * (3 << 20)   # more than the 1 MiB JSON cap of the other routes, the Manager bounds the text
        self.assertEqual(post("/api/fs/write", {"project": "team-x/app", "path": "a", "text": big, "sha": "ok"}, t)[0], 200)
        for path, n in (("/api/sessions/s1/send", 2 << 20), ("/api/fs/write", (6 << 20) + 1)):
            hdr = ("POST %s HTTP/1.1\r\nHost: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % (path, HOST, n))
            self.assertEqual(request(hdr.encode())[0], 413, path)
        self.assertEqual(post("/api/git/mr", {"project": "team-x/app", "title": "T", "draft": True}, t)[0], 200)
        self.assertIn(("mr", "T", True), piperd.MANAGER.created)
        self.assertEqual(post("/api/git/mr", {"project": "team-x/app", "title": "unpushed"}, t)[0], 409)
        self.assertEqual(post("/api/git/mr", {"project": "team-x/other", "title": "T"}, t)[0], 403)
        self.assertEqual(json.loads(post("/api/stack/restart", {"project": "team-x/app", "service": "db"}, t)[2]),
                         {"ok": True, "job": "j1"})
        self.assertEqual(post("/api/stack/restart", {"project": "team-x/app", "service": "bad name"}, t)[0], 409)
        self.assertEqual(json.loads(post("/api/stack/action", {"project": "team-x/app", "verb": "stop", "service": "db"}, t)[2])["job"]["verb"], "stop")
        self.assertEqual(post("/api/stack/action", {"project": "team-x/app", "verb": "purge"}, t)[0], 409)
        self.assertTrue(json.loads(post("/api/stack/cancel", {"project": "team-x/app", "id": "abcdef123456"}, t)[2])["signalled"])
        # F-STACK 2026-10-07: `piper attach`'s terminals-only token restarted / downed host containers
        with open(common.TOKENS_FILE, "a") as f:
            f.write("scoped-stack\tsession:s1\thook\n")
        for path, b in (("/api/stack/restart", {"project": "team-x/app", "service": "db"}),
                        ("/api/stack/action", {"project": "team-x/app", "verb": "down"}),
                        ("/api/stack/cancel", {"project": "team-x/app", "id": "abcdef123456"})):
            self.assertEqual(post(path, b, common.own_token("tty"))[0], 403, path)
            self.assertEqual(post(path, b, "scoped-stack")[0], 403, path)
        self.assertEqual(get("/api/stack?project=team-x%2Fapp", token=common.own_token("tty"))[0], 200)   # reading stays
        self.assertEqual(post("/api/fs/write", {"project": "team-x/app"}, t, origin="http://evil.example")[0], 403)

    def test_file_operations_and_file_turns(self):
        """-L9: the routes, the `*` GUI token, 404 hidden / 409 refused / 400 bad query."""
        t, pj = self.tok, "project=team-x/app"
        code, _, body = post("/api/fs/create", {"project": "team-x/app", "path": "n.txt", "kind": "file", "text": "x"}, t)
        self.assertEqual((code, json.loads(body)), (200, {"path": "n.txt", "sha": "s"}))
        self.assertIn(("create", "n.txt", "file", "x"), piperd.MANAGER.created)
        self.assertEqual(post("/api/fs/create", {"project": "team-x/app", "path": "exists", "kind": "file"}, t)[0], 409)
        self.assertEqual(post("/api/fs/create", {"project": "team-x/app", "path": "secret", "kind": "dir"}, t)[0], 404)
        self.assertEqual(json.loads(post("/api/fs/rename", {"project": "team-x/app", "from": "a", "to": "b"}, t)[2]),
                         {"from": "a", "to": "b"})
        self.assertEqual(post("/api/fs/rename", {"project": "team-x/app", "from": "a", "to": "secret/x"}, t)[0], 404)
        self.assertEqual(json.loads(post("/api/fs/delete", {"project": "team-x/app", "path": "a"}, t)[2])["trash"],
                         "20261006-120000-0123abcd")
        self.assertEqual(post("/api/fs/restore", {"project": "team-x/app", "trash": "20261006-120000-0123abcd"}, t)[0], 200)
        self.assertEqual(post("/api/fs/restore", {"project": "team-x/app", "trash": "../x"}, t)[0], 404)
        self.assertEqual(post("/api/fs/delete", {"project": "team-x/nope", "path": "a"}, t)[0], 404)
        big = "y" * (3 << 20)   # create takes 2 MiB of text like write (the 6 MiB JSON cap)
        self.assertEqual(post("/api/fs/create", {"project": "team-x/app", "path": "b", "kind": "file", "text": big}, t)[0], 200)
        # a token without the GUI's full scope (a session's, `piper attach`'s) never writes here
        with open(common.TOKENS_FILE, "a") as f:
            f.write("scoped-ops\tsession:s1\thook\n")
        n = len(piperd.MANAGER.created)
        for path, b in (("/api/fs/create", {"project": "team-x/app", "path": "z", "kind": "file"}),
                        ("/api/fs/rename", {"project": "team-x/app", "from": "a", "to": "z"}),
                        ("/api/fs/delete", {"project": "team-x/app", "path": "a"}),
                        ("/api/fs/restore", {"project": "team-x/app", "trash": "20261006-120000-0123abcd"})):
            self.assertEqual(post(path, b, "scoped-ops")[0], 403, path)
            self.assertEqual(post(path, b, common.own_token("tty"))[0], 403, path)
            self.assertEqual(post(path, b)[0], 401, path)
        self.assertEqual(len(piperd.MANAGER.created), n)
        r = json.loads(get("/api/sessions/s1/turns/file?path=src/A.java", token=t)[2])
        self.assertEqual(r, {"turns": [{"index": 0, "add": 1, "del": 0}], "diff_path": "src/A.java"})
        self.assertEqual(get("/api/sessions/s1/turns/file?path=secret", token=t)[0], 404)
        self.assertEqual(get("/api/sessions/zz/turns/file?path=a", token=t)[0], 404)

    def test_cross_origin_post_refused(self):
        self.assertEqual(post("/api/sessions", {"project": "x"}, self.tok, origin="http://evil.example")[0], 403)
        code, _, body = post("/api/sessions", {"project": "x", "task": "t"}, self.tok, origin="http://" + HOST)
        self.assertEqual(code, 202)
        self.assertEqual(piperd.MANAGER.created[-1], ("x", "t", "gui"))
        self.assertEqual(post("/api/sessions", {"project": "nope"}, self.tok)[0], 404)
        self.assertEqual(post("/api/sessions", {"project": "x", "agent": "codex"}, self.tok)[0], 202)
        self.assertEqual(piperd.MANAGER.created[-1], ("x", "", "gui", "codex"))

    def test_origin_tty_and_gui_polled(self):
        """`piper attach` creates sessions as origin tty, and its polling is not the GUI's."""
        post("/api/sessions", {"project": "x", "origin": "tty"}, self.tok)
        self.assertEqual(piperd.MANAGER.created[-1], ("x", "", "tty"))
        post("/api/sessions", {"project": "x", "origin": "advisor"}, self.tok)   # never chosen by a client
        self.assertEqual(piperd.MANAGER.created[-1], ("x", "", "gui"))
        n = piperd.MANAGER.polled
        get("/api/state", token=self.tok)
        self.assertEqual(piperd.MANAGER.polled, n + 1)
        tty = common.own_token("tty")
        self.assertEqual(common.own_token("tty"), tty)   # minted once, read from $PIPER_HOME/tty-token
        self.assertEqual(oct(os.stat(os.path.join(common.PIPER_HOME, "tty-token")).st_mode & 0o777), "0o600")
        self.assertEqual(get("/api/state", token=tty)[0], 200)
        self.assertEqual(piperd.MANAGER.polled, n + 1)

    def test_start_opts_gui_token_only(self):
        code, _, _ = post("/api/sessions", {"project": "x", "opts": {"docker": True}}, self.tok)
        self.assertEqual(code, 202)
        self.assertEqual(piperd.MANAGER.created[-1], ("x", "", "gui", {"docker": True}))
        for bad in (["docker"], {"docker": 1}, {"docker": "true"}):   # never a silent start without it
            self.assertEqual(post("/api/sessions", {"project": "x", "opts": bad}, self.tok)[0], 409, bad)
        with open(common.TOKENS_FILE, "a") as f:
            f.write("scoped-tok\tsession:s1\thook\n")
        n = len(piperd.MANAGER.created)
        code, _, body = post("/api/sessions", {"project": "x", "opts": {"unrestricted": True}}, "scoped-tok")
        self.assertEqual((code, json.loads(body)["error"]), (403, "start options need the GUI token"))
        self.assertEqual(post("/api/inbox/start", {"key": "k", "opts": {"docker": True}}, "scoped-tok")[0], 403)
        self.assertEqual(len(piperd.MANAGER.created), n)
        self.assertEqual(post("/api/sessions", {"project": "x"}, "scoped-tok")[0], 202)   # without opts as before

    def test_git_routes(self):
        code, _, body = get("/api/sessions/s1/git", token=self.tok)
        self.assertEqual((code, json.loads(body)["branch"]), (200, "main"))
        code, h, body = get("/api/sessions/s1/git?diff=src/a.txt&untracked=1", token=self.tok)
        self.assertEqual(body, b"diff for src/a.txt untracked=True")
        self.assertTrue(h["content-type"].startswith("text/plain"))
        self.assertEqual(get("/api/sessions/zz/git", token=self.tok)[0], 404)
        code, _, body = post("/api/sessions/s1/git/commit", {"message": "m", "files": ["a"]}, self.tok)
        self.assertEqual(code, 200)
        self.assertEqual(post("/api/sessions/s1/git/commit", {"message": "m", "files": "a"}, self.tok)[0], 409)
        self.assertEqual(get("/api/sessions/s1/git")[0], 401)

    def test_git_window_routes(self):
        """9.25: the Git window's reads are file content (GUI token), its branch / worktree / remote ops,
        amend and the drafted message are the GUI's only."""
        from unittest import mock
        t, tty, pj = self.tok, common.mint_token("tty"), "project=team-x%2Fapp"
        reads = [("/api/git/rows?%s&n=5&ref=main" % pj, "git_graph_rows", ("team-x/app", 5, "main")),
                 ("/api/git/branches?%s" % pj, "git_branches", ("team-x/app",)),
                 ("/api/git/commit?%s&commit=abcd" % pj, "git_commit_detail", ("team-x/app", "abcd")),
                 ("/api/git/blob?%s&commit=abcd&path=a&side=old" % pj, "git_blob", ("team-x/app", "abcd", "a", "old"))]
        for url, method, args in reads:
            with mock.patch.object(piperd.MANAGER, method, return_value={"ok": True}, create=True) as call:
                self.assertEqual(get(url)[0], 401, url)
                self.assertEqual(get(url, token=tty)[0], 403, url)
                self.assertEqual(get(url, token=t)[0], 200, url)
                call.assert_called_once_with(*args)
        with mock.patch.object(piperd.MANAGER, "git_graph_rows", create=True) as call:
            self.assertEqual(get("/api/git/rows?%s&n=x" % pj, token=t)[0], 400)
            call.assert_not_called()
        posts = [("/api/git/branch", {"project": "team-x/app", "action": "switch", "name": "b"}, "git_branch_op"),
                 ("/api/git/worktree", {"project": "team-x/app", "action": "add", "name": "w"}, "git_worktree_op"),
                 ("/api/git/fetch", {"project": "team-x/app"}, "git_remote_op"),
                 ("/api/git/pull", {"project": "team-x/app"}, "git_remote_op")]
        for url, body, method in posts:
            with mock.patch.object(piperd.MANAGER, method, return_value={"ok": True}, create=True) as call:
                self.assertEqual(post(url, body, tty)[0], 403, url)
                call.assert_not_called()
                self.assertEqual(post(url, body, t)[0], 200, url)
                self.assertEqual(call.call_args[0][0], "team-x/app")
                if method == "git_remote_op":
                    self.assertEqual(call.call_args[0][1], url.rsplit("/", 1)[1])
                else:
                    self.assertEqual(call.call_args[0][1], body)
        with mock.patch.object(piperd.MANAGER, "git_commit", return_value={"commit": "abc"}) as call:
            body = {"message": "m", "files": [], "amend": True}
            self.assertEqual(post("/api/sessions/s1/git/commit", body, tty)[0], 403)
            call.assert_not_called()
            self.assertEqual(post("/api/sessions/s1/git/commit", body, t)[0], 200)
            call.assert_called_once_with("s1", "m", [], None, amend=True)   # D14: always the reviewed path
        with mock.patch.object(piperd.MANAGER, "git_message", return_value={"message": "fix: x"}, create=True) as call:
            self.assertEqual(post("/api/sessions/s1/git/message", {"files": ["a"]}, tty)[0], 403)
            self.assertEqual(post("/api/sessions/s1/git/message", {"files": "a"}, t)[0], 409)
            call.assert_not_called()
            code, _, out = post("/api/sessions/s1/git/message", {"files": ["a"]}, t)
            self.assertEqual((code, json.loads(out)["message"]), (200, "fix: x"))
            call.assert_called_once_with("s1", ["a"])
        with mock.patch.object(piperd.MANAGER, "mr_message", return_value={"title": "t"}, create=True) as call:
            self.assertEqual(post("/api/sessions/s1/git/mr-message", {"target": "main"}, tty)[0], 403)
            call.assert_not_called()
            code, _, out = post("/api/sessions/s1/git/mr-message", {"target": "main"}, t)
            self.assertEqual((code, json.loads(out)["title"]), (200, "t"))
            call.assert_called_once_with("s1", "main")

    def test_drop_route(self):
        raw = b"\x89PNG"
        hdr = ("POST /api/sessions/s1/drop HTTP/1.1\r\nHost: %s\r\nAuthorization: Bearer %s\r\n"
               "Content-Type: image/png\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % (HOST, self.tok, len(raw)))
        code, _, body = request(hdr.encode() + raw)
        self.assertEqual((code, json.loads(body)["path"]), (200, "/home/.claude/piper-drop/x.png"))
        hdr = hdr.replace("image/png", "text/html")
        self.assertEqual(request(hdr.encode() + raw)[0], 409)
        big = ("POST /api/sessions/s1/drop HTTP/1.1\r\nHost: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
               % (HOST, (10 << 20) + 1))
        self.assertEqual(request(big.encode())[0], 413)

    def test_boot_has_dashboard_link(self):
        self.assertIn(b'"dashboardUrl": ""', get("/")[2])   # no stack provider (an extension's)
        with mock.patch.object(piperd.MANAGER, "_stack_dash", return_value="http://127.0.0.1:1/", create=True):
            self.assertIn(b'"dashboardUrl": "http://127.0.0.1:1/"', get("/")[2])

    def test_boot_paints_the_chosen_theme(self):
        # N1: the first frame uses the policy's theme - the poll would otherwise repaint dark -> light
        m = piperd.MANAGER
        old = getattr(m, "policy", None)
        try:
            for policy, want in ((None, b'"theme": "dark"'), ({"theme": "light"}, b'"theme": "light"')):
                m.policy = policy
                self.assertIn(want, get("/")[2], policy)
        finally:
            m.policy = old

    def test_piper_themes_are_independent_of_dashboard(self):
        self.assertEqual(piperd.THEMES, os.path.join(piperd.WEB, "themes.json"))
        code, _, body = get("/piper/themes.json")
        self.assertEqual(code, 200)
        themes = json.loads(body)
        boot = json.loads(re.search(rb"window\.__PIPER__=(.*?)</script>", get("/")[2]).group(1))
        self.assertEqual(boot["themes"], themes)
        self.assertEqual(themes["themes"]["dark"]["tokens"]["page"], "#0c0d0f")
        self.assertEqual(themes["themes"]["light"]["tokens"]["page"], "#f5f5f6")

    def test_static_allowlist(self):
        self.assertEqual(get("/piper/dist/main.js")[0], 200)
        self.assertEqual(get("/piper/vendor/xterm/xterm.mjs")[1]["content-type"], "text/javascript; charset=utf-8")
        self.assertEqual(get("/piper/vendor/codemirror/cm.mjs")[1]["content-type"], "text/javascript; charset=utf-8")
        self.assertEqual(get("/piper/themes.json")[0], 200)
        for bad in ("/piper/src/main.ts", "/piper/../piperd.py", "/piper/%2e%2e/piperd.py", "/piper/tsconfig.json",
                    "/piper/vendor/xterm/typings/xterm.d.ts", "/piper/vendor/codemirror/typings/cm.d.ts",
                    "/piper/dist/../../piperd.py"):
            self.assertEqual(get(bad)[0], 404, bad)


def ws_frame(payload: bytes, op=wsproto.OP_TEXT) -> bytes:
    mask = os.urandom(4)
    n = len(payload)
    head = struct.pack("!BB", 0x80 | op, 0x80 | n) if n < 126 else struct.pack("!BBH", 0x80 | op, 0x80 | 126, n)
    return head + mask + bytes(b ^ mask[i & 3] for i, b in enumerate(payload))


def ws_read(sock) -> tuple[int, bytes]:
    def exact(n):
        buf = b""
        while len(buf) < n:
            c = sock.recv(n - len(buf))
            if not c:
                raise EOFError
            buf += c
        return buf
    b1, b2 = exact(2)
    n = b2 & 0x7F
    if n == 126:
        (n,) = struct.unpack("!H", exact(2))
    return b1 & 0x0F, exact(n)


class PiperdWebSocketTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        piperd.MANAGER = FakeManager()
        cls.tok = common.mint_token("test")
        cls.ph = _env.InThreadPtyHost()
        piperd.pty_client = lambda on_event: cls.ph.client(on_event=on_event)

    @classmethod
    def tearDownClass(cls):
        cls.ph.stop()

    def open(self, sid="s1", origin="http://" + HOST, host=HOST):
        a, b = socket.socketpair()
        self.addCleanup(b.close)
        b.settimeout(10)
        threading.Thread(target=serve, args=(a,), daemon=True).start()
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /ws/%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n" % (sid, host, key))
        if origin:
            req += "Origin: %s\r\n" % origin
        b.sendall((req + "\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += b.recv(1)
        return b, head.decode(), key

    def add_session(self, s):
        """Rebinds host.sessions instead of resizing it: ptyhost's loop thread iterates it (_reap),
        and a resize under it kills the loop - every later call then times out."""
        h = self.ph.host
        self.assertTrue(self.ph.thread.is_alive(), "ptyhost's loop died in an earlier test")
        h.sessions = dict(h.sessions, **{s.id: s})
        self.addCleanup(lambda: setattr(h, "sessions", {k: v for k, v in h.sessions.items() if k != s.id}))

    def test_handshake_and_bad_auth(self):
        b, head, key = self.open()
        self.assertIn("101", head.split("\r\n")[0])
        self.assertIn("Sec-WebSocket-Accept: %s" % wsproto.accept_key(key), head)
        b.sendall(ws_frame(json.dumps({"auth": "wrong"}).encode()))
        op, payload = ws_read(b)
        self.assertEqual(op, wsproto.OP_CLOSE)
        self.assertEqual(struct.unpack("!H", payload[:2])[0], 4401)

    def test_remote_host_upgrades_and_still_needs_auth(self):
        """9.56: wss through `tailscale serve` - the upgrade passes, a silent client is closed within 3 s."""
        import time
        ts = "mac.tail-example.ts.net"
        with mock.patch.dict(os.environ, {"PIPER_ALLOWED_HOSTS": ts}):
            _, head, _ = self.open(origin="http://" + ts, host=ts)
            self.assertIn("403", head.split("\r\n")[0])
            b, head, _ = self.open(origin="https://" + ts, host=ts)
            self.assertIn("101", head.split("\r\n")[0])
            t0 = time.monotonic()
            op, payload = ws_read(b)
            self.assertEqual((op, struct.unpack("!H", payload[:2])[0]), (wsproto.OP_CLOSE, 4401))
            self.assertLess(time.monotonic() - t0, 5)
            b, _, _ = self.open(origin="https://" + ts, host=ts)
            b.sendall(ws_frame(json.dumps({"auth": self.tok}).encode()))
            self.assertNotEqual(ws_read(b)[0], wsproto.OP_CLOSE)

    def test_origin_required_and_unknown_session(self):
        _, head, _ = self.open(origin=None)
        self.assertIn("403", head.split("\r\n")[0])
        _, head, _ = self.open(sid="zz")
        self.assertIn("404", head.split("\r\n")[0])

    def test_auth_ok_reaches_ptyhost(self):
        b, _, _ = self.open()
        b.sendall(ws_frame(json.dumps({"auth": self.tok}).encode()))
        op, payload = ws_read(b)
        self.assertEqual(op, wsproto.OP_TEXT)
        msg = json.loads(payload)
        # the fake manager knows s1, the in-thread ptyhost does not: the relay answers with its error
        self.assertEqual(msg["t"], "error")
        self.assertIn("unknown session", msg["error"])


    def test_tty_size_and_presence_reach_the_browser(self):
        """a native terminal attached with `piper attach` sizes the PTY: the browser hears it."""
        import ptyhost
        s = ptyhost.Session("s1", 0, -1, 100, 30, None, False)   # no child: resize only records the size
        self.add_session(s)
        b, _, _ = self.open()
        b.sendall(ws_frame(json.dumps({"auth": self.tok}).encode()))
        hello = json.loads(ws_read(b)[1])
        self.assertEqual((hello["t"], hello["info"]["tty"]), ("hello", 0))
        term = self.ph.client()
        self.addCleanup(term.close)
        self.assertTrue(term.call("subscribe", id="s1", tty=True)["ok"])
        term.call("resize", id="s1", cols=180, rows=50)
        got = []
        while len(got) < 2:
            op, payload = ws_read(b)
            if op == wsproto.OP_TEXT:
                got.append(json.loads(payload))
        self.assertEqual(got, [{"t": "tty", "n": 1}, {"t": "size", "c": 180, "r": 50}])
        b.sendall(ws_frame(json.dumps({"t": "resize", "c": 90, "r": 25}).encode()))   # the browser's fit: ignored
        term.call("list")   # a round trip after the browser's request
        self.assertEqual((s.cols, s.rows), (180, 50))
        term.call("unsubscribe", id="s1")
        op, payload = ws_read(b)
        self.assertEqual(json.loads(payload), {"t": "tty", "n": 0})


    def test_use_here_detaches_the_terminal(self):
        """"Use here" in the GUI: the attached terminal gets tty-taken, the browser sizes again."""
        import ptyhost
        s = ptyhost.Session("s1", 0, -1, 100, 30, None, False)
        self.add_session(s)
        got_term, taken = [], threading.Event()

        def on_term(e):
            got_term.append(e)
            if e.get("ev") == "tty-taken":
                taken.set()
        term = self.ph.client(on_event=on_term)
        self.addCleanup(term.close)
        term.call("subscribe", id="s1", tty=True)
        term.call("resize", id="s1", cols=180, rows=50)
        b, _, _ = self.open()
        b.sendall(ws_frame(json.dumps({"auth": self.tok}).encode()))
        self.assertEqual(json.loads(ws_read(b)[1])["info"]["tty"], 1)
        b.sendall(ws_frame(json.dumps({"t": "take"}).encode()))
        self.assertEqual(json.loads(ws_read(b)[1]), {"t": "tty", "n": 0})
        # N9: the terminal's events arrive on its own reader thread - under load after the browser's reply
        self.assertTrue(taken.wait(10), [e.get("ev") for e in got_term])
        self.assertIsNone(s.tty)
        b.sendall(ws_frame(json.dumps({"t": "resize", "c": 90, "r": 25}).encode()))
        op, payload = ws_read(b)
        self.assertEqual(json.loads(payload), {"t": "size", "c": 90, "r": 25})   # the browser's size applies again


class WsProtoTest(unittest.TestCase):
    def test_rfc_accept_vector(self):
        self.assertEqual(wsproto.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_fragments_ping_and_unmasked(self):
        a, b = socket.socketpair()
        b.settimeout(10)
        ws = wsproto.WebSocket(a)
        mask = b"\x01\x02\x03\x04"
        def masked(fin, op, data):
            return bytes([(0x80 if fin else 0) | op, 0x80 | len(data)]) + mask + bytes(x ^ mask[i & 3] for i, x in enumerate(data))
        b.sendall(masked(False, wsproto.OP_TEXT, b"hel") + masked(True, wsproto.OP_PING, b"p") + masked(True, wsproto.OP_CONT, b"lo"))
        self.assertEqual(ws.recv(), (wsproto.OP_TEXT, b"hello"))
        self.assertEqual(ws_read(b), (wsproto.OP_PONG, b"p"))
        b.sendall(bytes([0x81, 3]) + b"abc")   # unmasked client frame
        with self.assertRaises(wsproto.WSProtocolError):
            ws.recv()


class StampedLogTest(unittest.TestCase):
    def test_every_log_line_starts_with_the_time(self):
        """piperd.log had no times, so a hang could not be placed in time."""
        import io
        raw = io.StringIO()
        log = piperd.StampedStream(raw, clock=lambda: time.struct_time((2026, 10, 16, 15, 4, 5, 0, 0, -1)))
        print("piperd: listening", file=log)
        log.write("Traceback\n  line 1\npart")
        log.write("ial\n")
        log.flush()
        self.assertEqual(raw.getvalue(), "2026-10-16 15:04:05 piperd: listening\n"
                                         "2026-10-16 15:04:05 Traceback\n"
                                         "2026-10-16 15:04:05   line 1\n"
                                         "2026-10-16 15:04:05 partial\n")
        self.assertFalse(log.isatty())   # the rest of the stream is the real one's


if __name__ == "__main__":
    unittest.main()
