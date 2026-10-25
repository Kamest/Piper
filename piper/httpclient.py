"""HTTP requests in IntelliJ's .http format, run on the HOST by JetBrains' `ijhttp` CLI
(the same engine as the HTTP Client in IntelliJ IDEA).

Two ways in, one runner:
  - the GUI file browser: the user clicks ▶ on a request or "Run all" - the click is the
    approval (prod asks once more in the page);
  - a session's MCP tools http_envs / http_run / http_last_result: the sandbox never sees an
    env file (http-client/http-client.env.json is a policy candidate), piperd runs the file
    with the real environment and answers with a scrubbed result. dev runs at once, test asks
    on a card ("Allow for this session"), prod asks on a card every time (ALWAYS_ASK).

What an agent-written file cannot do here (http_run, `_agent_checks`):
  - send to a host that is not in the environment's values, while the file (or a file it
    references) differs from git HEAD - a response handler runs JavaScript on the host with
    the secrets in reach, so an edited file must not be able to talk to anything else;
  - read files outside the project or hidden by the isolation policy (`< ./x`, `> ./x.js`),
    or use {{$dotenv}} / {{$processEnv}} (piperd's own environment);
  - see Authorization / Cookie values or any secret of the environment in what comes back
    (also base64- and URL-encoded: an echo endpoint returns `Basic dXNlcjpwYXNz`).

ijhttp facts this relies on (verified with build 261.25134.95, 2026-10-05):
  - `-V k=v` overrides a value of the env file; globals are NOT kept between two runs;
  - `client.log` output appears nowhere; a request has ONE response handler (a second
    `> {% %}` is ignored); a client.test name lands in the JUnit report - that is how the
    globals a run set are carried to the next single-request run (`instrument`);
  - `-L VERBOSE` prints each request and response, private env values masked as {{**x**}};
  - exit 1 when a test or a request failed.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import os
import re
import secrets as _secrets
import shutil
import ssl
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

import common
import evidence
import gitops
import guard
import projectfs
from common import audit, read_json, write_json

ENV_FILE = "http-client.env.json"
PRIVATE_FILE = "http-client.private.env.json"
META_KEY = "piper"            # an environment's {"piper": {"tunnel": "test/<target>", "port"?}} - Piper's, not a variable
SSL_KEY = "SSLConfiguration"  # IDEA's per-environment TLS settings: {"verifyHostCertificate": false} -> --insecure
CLASSES = ("dev", "test", "prod")
MASK = "••••"
SAVE_DIR = "development"      # a bare save_to name lands here (the unversioned scratch dir of a project)
MAX_SAVE = 50 << 20           # a response saved to a file
TEXT_CT_RX = re.compile(r"^(text/|application/(json|xml|javascript|ecmascript|x-www-form-urlencoded|x-ndjson|yaml|x-yaml|toml"
                        r"|graphql|sql|[\w.+-]*\+(json|xml|yaml))"
                        r"|multipart/form-data)", re.I)
HTTP_DIR = os.path.join(common.PIPER_HOME, "http")
RUNS_DIR = os.path.join(HTTP_DIR, "runs")
GLOBALS_FILE = os.path.join(HTTP_DIR, "globals.json")
HOSTS_FILE = os.path.join(HTTP_DIR, "hosts.json")   # {project key: [hosts]} allowed on an http_host card
SEEN_HOSTS_FILE = os.path.join(HTTP_DIR, "seen-hosts.json")
# {project key: {"<env dir>\0<env>": {"tunnel": {...}, "verify_tls": bool}}} - the GUI's environment settings:
# host-only (the sandbox never sees ~/.piper), so they need no "hidden env file" rule and never go into git
ENVCFG_FILE = os.path.join(HTTP_DIR, "envcfg.json")
TUNNEL_KINDS = ("auto", "none", "ssh", "stack")
KEEP_RUNS = 300
CARD_S = 600            # one card of a session's run...
LOCK_WAIT_AGENT = 30    # ...at most this for another run of the project...
AGENT_RUN_MAX = 240     # ...then the run: together inside piper-mcp's 900 s for a confirmed tool
MAX_FILE = 1 << 20            # a .http file / an env file
MAX_BODY_GUI = 512 << 10      # a response body kept for the GUI
DEFAULTS = {
    "timeout": 180,           # s, one ijhttp run
    "body_limit": 4000,       # characters of a response body an agent gets
    "insecure": ["dev"],      # classes run with --insecure (self-signed dev certificates)
    "card_dev": False,        # a card for a session's dev run too
    "classes": {},            # {project key: {env name: dev|test|prod}} - overrides the name rule
}
IJHTTP_URL = "https://jb.gg/ijhttp/latest"
SKIP_DIRS = {"node_modules", ".git", ".idea", ".gradle", ".next", ".pnpm-store", "target", "build", "dist"}
METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT")
VAR_RX = re.compile(r"^@([A-Za-z_][\w.-]*)\s*=\s*(.*?)\s*$")
NAME_RX = re.compile(r"^(?:#|//)\s*@name\s*=?\s*(.+?)\s*$")
REQ_RX = re.compile(r"^(?:(%s)\s+)?(\S.*?)(?:\s+HTTP/[\d.]+)?\s*$" % "|".join(METHODS))
REF_RX = re.compile(r"^<@?\s+(?!\{%)(\S.*?)\s*$")          # `< body.json`, `<@ body.json` (with {{vars}})
SUBST_REF_RX = re.compile(r"^<@\s+")
SAVE_RX = re.compile(r"^>>!?\s+(\S.*?)\s*$")                   # `>> out.json`: ijhttp writes the response there
HANDLER_FILE_RX = re.compile(r"^>\s+(?!\{%)(\S.*?)\s*$")
GLOBAL_SET_RX = re.compile(r"client\.global\.set\(\s*(['\"])([^'\"\\]{1,100})\1")
VAR_USE_RX = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
SCRIPT_RX = re.compile(r"\{%|^\s*>\s+(?!\{%)\S", re.M)   # an inline script or a handler file
FORBIDDEN_VARS = re.compile(r"\{\{\s*\$(dotenv|processEnv)\b", re.I)
SECRET_KEY_RX = re.compile(r"pass|pwd|secret|token|key|auth|credential|cookie|session|bearer", re.I)
SECRET_HEADERS = re.compile(r"^(authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-auth-token|api-key)$", re.I)
B64_TOKEN_RX = re.compile(r"[A-Za-z0-9+/_-]{12,}={0,2}")
HOST_RX = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]*$|^localhost$|^\d{1,3}(?:\.\d{1,3}){3}$", re.I)
CAPTURE = "__piper_g__"
# `-L VERBOSE` (needed for the bodies) prints this for a binary response and then CANCELS the run: exit 1,
# no JUnit report, the file's later requests never run (ijhttp 2026.1, verified with a PDF response)
BINARY_MARK = "<Binary response body is ignored"


_STORE_LOCK = threading.Lock()   # read-modify-write of globals.json / hosts.json


class HttpError(ValueError):
    """A refused or failed run (piperd answers 409 / the tool answers an error)."""


# ------------------------------------------------------------------ the .http file
def parse(text: str) -> dict:
    """Requests of an IntelliJ .http file: name, method, url, the request line, the line span of
    the block, headers, referenced files, response handler span. `vars`: the file variables
    (@name = value) in file order, with their line numbers."""
    lines = text.split("\n")
    blocks, start = [], 0
    for i, ln in enumerate(lines):
        if ln.startswith("###") and i > start:
            blocks.append((start, i))
            start = i
        elif ln.startswith("###"):
            start = i
    blocks.append((start, len(lines)))
    out, fvars = [], []
    for b0, b1 in blocks:
        title = lines[b0][3:].strip() if lines[b0].startswith("###") else ""
        req = {"name": title, "start": b0, "end": b1, "line": None, "method": "GET", "url": "",
               "headers": [], "files": [], "handler": None, "handler_file": None, "pre": None,
               "scripts": [], "subst": [], "saves": [], "body": ""}
        i = b0 + (1 if lines[b0].startswith("###") else 0)
        # before the request line: comments, @name, file variables, a pre-request script
        while i < b1:
            ln = lines[i].strip()
            m = NAME_RX.match(ln)
            if m:
                req["name"] = req["name"] or m.group(1)
            elif VAR_RX.match(ln):
                v = VAR_RX.match(ln)
                fvars.append({"line": i, "name": v.group(1), "value": v.group(2)})
            elif ln.startswith("< {%"):
                j = i
                while j < b1 and "%}" not in lines[j].strip()[(4 if j == i else 0):]:
                    j += 1
                req["pre"] = (i, min(j, b1 - 1))
                i = j
            elif REF_RX.match(ln):   # a pre-request script file
                req["files"].append(REF_RX.match(ln).group(1))
                req["scripts"].append(REF_RX.match(ln).group(1))
            elif ln and not ln.startswith("#") and not ln.startswith("//"):
                break
            i += 1
        if i >= b1:
            continue   # no request in this block (the variables above the first ###)
        m = REQ_RX.match(lines[i].strip())
        if not m:
            continue
        req["line"], req["method"], url = i, (m.group(1) or "GET"), m.group(2)
        i += 1
        while i < b1 and lines[i][:1] in (" ", "\t") and lines[i].strip()[:1] in ("?", "&"):
            url += lines[i].strip()
            i += 1
        req["url"] = url
        while i < b1 and lines[i].strip():   # headers
            h = lines[i].strip()
            if ":" in h and not h.startswith("#") and not h.startswith("//"):
                k, v = h.split(":", 1)
                req["headers"].append([k.strip(), v.strip()])
            i += 1
        body, in_body = [], True
        while i < b1:   # body, handler, response reference
            ln = lines[i].strip()
            if in_body and (ln.startswith("> ") or ln.startswith(">>") or ln.startswith("<> ")):
                in_body = False
            if in_body:
                body.append(lines[i])
            if ln.startswith("> {%"):
                j = i
                while j < b1 and "%}" not in lines[j].strip()[(4 if j == i else 0):]:
                    j += 1
                req["handler"] = (i, min(j, b1 - 1))
                i = j
            elif SAVE_RX.match(ln):
                req["saves"].append(SAVE_RX.match(ln).group(1))
            elif HANDLER_FILE_RX.match(ln):
                req["handler_file"] = HANDLER_FILE_RX.match(ln).group(1)
                req["files"].append(req["handler_file"])
                req["scripts"].append(req["handler_file"])
            elif REF_RX.match(ln):
                req["files"].append(REF_RX.match(ln).group(1))
                if SUBST_REF_RX.match(ln):
                    req["subst"].append(REF_RX.match(ln).group(1))
            i += 1
        while body and not body[-1].strip():
            body.pop()
        while body and not body[0].strip():
            body.pop(0)
        req["body"] = "\n".join(body)
        req["index"] = len(out)
        req["name"] = req["name"] or "#%d" % (len(out) + 1)
        out.append(req)
    return {"requests": out, "vars": fvars}


def requests_brief(parsed: dict) -> list[dict]:
    """What the GUI gutter and the tools list: index, name, method, url, request line."""
    return [{"index": r["index"], "name": r["name"], "method": r["method"], "url": r["url"],
             "line": r["line"], "start": r["start"], "headers": r["headers"], "body": r["body"][:4000],
             "files": r["files"], "script": bool(r["handler"] or r["handler_file"] or r["pre"] or r["scripts"])}
            for r in parsed["requests"]]


def global_names(text: str) -> list[str]:
    return sorted({m.group(2) for m in GLOBAL_SET_RX.finditer(text)})


def global_setters(text: str, parsed: dict) -> dict:
    """{global name: the first request whose handler sets it}."""
    lines, out = text.split("\n"), {}
    for r in parsed["requests"]:
        for m in GLOBAL_SET_RX.finditer("\n".join(lines[r["start"]:r["end"]])):
            out.setdefault(m.group(2), r)
    return out


def instrument(text: str, parsed: dict, names: list[str]) -> str:
    return "\n".join(t for t, _ in instrument_rows([(ln, i) for i, ln in enumerate(text.split("\n"))], names, parsed))


def instrument_rows(rows: list, names: list[str], parsed: dict | None = None) -> list:
    """(text, original line) rows with one extra client.test per request whose NAME carries the
    values of the globals the file sets (ijhttp prints client.log nowhere; the JUnit report has
    test names). Into an existing handler on its `%}` line (no line added); a request without one
    gets two lines (a blank, `> {% … %}`). A request with a handler file (> ./x.js) is left alone."""
    if not names:
        return rows
    parsed = parsed or parse("\n".join(t for t, _ in rows))
    obj = ", ".join("%s: client.global.get(%s)" % (json.dumps(n), json.dumps(n)) for n in names)
    code = ';client.test("%s" + JSON.stringify({%s}), function () {});' % (CAPTURE, obj)
    rows = list(rows)
    edits = []   # (row, kind) applied bottom-up
    for r in parsed["requests"]:
        if r["handler_file"]:
            continue
        if r["handler"]:
            edits.append((r["handler"][1], "into"))
        else:
            last = r["line"]
            for k in range(r["line"], r["end"]):
                t = rows[k][0].strip()
                if t and not t.startswith("###") and not t.startswith("#") and not t.startswith("//"):
                    last = k
            edits.append((last, "after"))
    for ln, kind in sorted(edits, reverse=True):
        if kind == "into":
            t, o = rows[ln]
            cut = t.rfind("%}")
            rows[ln] = (t[:cut] + code + " " + t[cut:], o)
        else:
            rows[ln + 1:ln + 1] = [("", None), ("> {% " + code + " %}", None)]
    return rows


def single(text: str, parsed: dict, index: int) -> str:
    return "\n".join(t for t, _ in single_rows(text, parsed, index))


def single_rows(text: str, parsed: dict, index: int) -> list:
    """One request of the file on its own, with every file variable (@x = ...) above it, as
    (text, original line) rows."""
    lines = text.split("\n")
    r = parsed["requests"][index]
    head = [(lines[v["line"]], v["line"]) for v in parsed["vars"] if not (r["start"] <= v["line"] < r["end"])]
    body = [(lines[k], k) for k in range(r["start"], r["end"])]
    if not body or not body[0][0].startswith("###"):
        body = [("### " + r["name"], r["start"])] + body
    return head + [("", None)] + body


def cookie_jar_rows(rows: list) -> list:
    """Honour an explicit @no-cookie-jar even after a pre-request script (ijhttp 2026.1 ignores it there).
    Move only standalone directives in the preamble, keeping their source lines and all cookie headers.
    This only suppresses saving response cookies; ijhttp still sends cookies already in its jar."""
    parsed = parse("\n".join(t for t, _ in rows))
    out = list(rows)
    for r in reversed(parsed["requests"]):
        start = r["start"] + int(rows[r["start"]][0].startswith("###"))
        directive = first_script = None
        i = start
        while i < r["line"]:
            ln = rows[i][0].strip()
            if ln.startswith("< {%"):
                if first_script is None:
                    first_script = i
                first = i
                while i < r["line"] and "%}" not in rows[i][0].strip()[4 if i == first else 0:]:
                    i += 1
            elif REF_RX.match(ln):
                if first_script is None:
                    first_script = i
            elif directive is None and re.fullmatch(r"(?:#|//)\s*@no-cookie-jar", ln):
                directive = i
            i += 1
        if directive is not None and first_script is not None and directive > first_script:
            out.insert(first_script, out.pop(directive))
    return out


def relink(msg: str, run_file: str, rel: str, linemap: list) -> str:
    """ijhttp names the run's copy (`…/tmp/<id>/api.http:29`): the original file and its line."""
    def sub(m: re.Match) -> str:
        n = int(m.group(1)) - 1
        while 0 <= n < len(linemap) and linemap[n] is None:
            n -= 1
        return "%s:%d" % (rel, linemap[n] + 1) if 0 <= n < len(linemap) else rel
    msg = re.sub(re.escape(run_file) + r":(\d+)", sub, msg)
    return msg.replace(run_file, rel).replace(os.path.dirname(run_file) + "/", "")


def rewrite_refs(text: str, mapping: dict) -> str:
    """`< ./x` and `> ./x.js` lines pointing at the copies piperd made (the run's snapshot)."""
    out = []
    for ln in text.split("\n"):
        s = ln.strip()
        m = None if SAVE_RX.match(s) else (REF_RX.match(s) or HANDLER_FILE_RX.match(s))
        if m and m.group(1) in mapping:
            ln = ln[:ln.index(m.group(1))] + mapping[m.group(1)] + ln[ln.index(m.group(1)) + len(m.group(1)):]
        out.append(ln)
    return "\n".join(out)


# ------------------------------------------------------------------ environments
def classify(name: str, override: str | None = None) -> str:
    """dev | test | prod by the environment's name; anything unknown is prod (fail closed)."""
    if override in CLASSES:
        return override
    n = name.lower()
    if re.search(r"(^|[^a-z])(prod|production|live%s)([^a-z]|$)" % common.prod_words_rx(), n):
        return "prod"
    if re.search(r"(^|[^a-z])(test|testing|stage|staging|uat|qa|preprod)([^a-z]|$)", n):
        return "test"
    if re.search(r"(^|[^a-z])(dev|devel|development|local|localhost|docker)([^a-z]|$)", n):
        return "dev"
    return "prod"


def flatten(env: dict, prefix: str = "") -> dict:
    """{"api": {"domain": "x"}} -> {"api": "{...}", "api.domain": "x"} (ijhttp resolves both)."""
    out = {}
    for k, v in (env or {}).items():
        if not prefix and k in (META_KEY, SSL_KEY) and isinstance(v, dict):   # settings of the environment, not variables
            continue
        key = prefix + str(k)
        if isinstance(v, dict):
            out[key] = json.dumps(v, ensure_ascii=False)
            out.update(flatten(v, key + "."))
        elif v is not None:
            out[key] = v if isinstance(v, str) else json.dumps(v)
    return out


def substitute(s: str, values: dict, depth: int = 5) -> str:
    """{{name}} -> value (dotted names, nested values resolved again); unknown names stay."""
    for _ in range(depth):
        new = VAR_USE_RX.sub(lambda m: values.get(m.group(1), m.group(0)), s)
        if new == s:
            break
        s = new
    return s


def hosts_in(value: str) -> set[str]:
    """Host names a value names: a URL's host, or the value itself when it looks like one."""
    out = set()
    for m in re.finditer(r"[a-z][a-z0-9+.-]*://([^/\s:?#]+)", value, re.I):
        out.add(m.group(1).lower())
    v = value.strip().lower()
    if HOST_RX.match(v):
        out.add(v)
    return out


def url_host(url: str) -> str | None:
    u = url if "://" in url else "http://" + url
    try:
        h = urllib.parse.urlsplit(u).hostname
    except ValueError:
        return None
    return h.lower() if h and "{{" not in h and "}}" not in h else None


def leaves(env: dict, prefix: str = "") -> dict:
    """The scalar values of an environment by dotted name."""
    out = {}
    for k, v in (env or {}).items():
        if not prefix and k in (META_KEY, SSL_KEY) and isinstance(v, dict):
            continue
        if isinstance(v, dict):
            out.update(leaves(v, prefix + str(k) + "."))
        elif v is not None:
            out[prefix + str(k)] = v if isinstance(v, str) else json.dumps(v)
    return out


def is_locator(v: str) -> bool:
    """A bare host or a URL without credentials, query or fragment - the one kind of value of a hidden
    env file a session may see (it is in every request line anyway)."""
    v = v.strip()
    if HOST_RX.match(v):
        return True
    if not re.match(r"^[a-z][a-z0-9+.-]*://\S+$", v, re.I):
        return False
    try:
        u = urllib.parse.urlsplit(v)
    except ValueError:
        return False
    return bool(u.hostname) and not (u.username or u.password or u.query or u.fragment)


def secret_values(public: dict, private: dict, public_hidden: bool = False) -> list[str]:
    """Every private value, and public values under a secret-like key - longest first. A public env
    file the sandbox cannot see (a policy candidate): every value but a plain host/URL is a secret."""
    vals = set(leaves(private).values())
    vals |= {v for k, v in leaves(public).items()
             if (not is_locator(v) if public_hidden else SECRET_KEY_RX.search(k.rsplit(".", 1)[-1]))}
    return sorted((v for v in vals if len(v) >= 4), key=len, reverse=True)


def redactor(secret_list: list[str]):
    """text -> text with each secret masked, also base64 / URL-encoded, and any base64 token
    that decodes to something containing one (Basic user:pass)."""
    variants = set()
    for s in secret_list:
        b = s.encode()
        variants |= {s, urllib.parse.quote(s, safe=""), urllib.parse.quote_plus(s),
                     base64.b64encode(b).decode().rstrip("="), base64.urlsafe_b64encode(b).decode().rstrip("=")}
    variants = sorted((v for v in variants if len(v) >= 4), key=len, reverse=True)

    def b64_hit(m: re.Match) -> str:
        tok = m.group(0)
        try:
            raw = base64.b64decode(tok.replace("-", "+").replace("_", "/") + "=" * (-len(tok) % 4), validate=False)
            dec = raw.decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return tok
        return MASK if any(s in dec for s in secret_list) else tok

    def scrub(text: str) -> str:
        if not text or not secret_list:
            return text or ""
        for v in variants:
            text = text.replace(v, MASK)
        return B64_TOKEN_RX.sub(b64_hit, text)
    return scrub


def cookie_values(reqs: list[dict]) -> list[str]:
    """Values of the cookies a run received or sent - session credentials, masked like secrets."""
    out = set()
    for r in reqs:
        for k, v in r.get("headers", []) + r.get("req_headers", []):
            if k.lower() == "set-cookie":
                v = v.split(";", 1)[0]
            elif k.lower() != "cookie":
                continue
            for part in v.split(";"):
                val = part.split("=", 1)[1].strip() if "=" in part else ""
                if len(val) >= 8:
                    out.add(val)
                    out.add(urllib.parse.unquote(val))
    return sorted(out, key=len, reverse=True)


def mask_headers(headers: list) -> list:
    return [[k, MASK if SECRET_HEADERS.match(k) or SECRET_KEY_RX.search(k) and k.lower() != "content-length" else v]
            for k, v in headers]


# ------------------------------------------------------------------ tunnels, hints, a saved response
LOCAL_PORT_RX = r"(?<![\w.-])(localhost|127\.0\.0\.1):%s(?!\d)"
# a convention seen in the wild: the environment's NAME is the tunnel command (`test with tunnel `ssh -L 8443:192.0.2.1:443 dev@host``)
SSH_NAME_RX = re.compile(r"\bssh\s+(?:-p\s*(\d{1,5})\s+)?-L\s*(\d{2,5}):([A-Za-z0-9][A-Za-z0-9._-]*):(\d{1,5})"
                         r"(?:\s+-p\s*(\d{1,5}))?\s+(?:([A-Za-z_][A-Za-z0-9_.-]{0,31})@)?([A-Za-z0-9][A-Za-z0-9._-]*)")


def env_meta(public: dict, private: dict, env: str) -> tuple[dict, dict]:
    """(Piper's "piper" object, IDEA's "SSLConfiguration") of an environment - not variables."""
    out = []
    for key in (META_KEY, SSL_KEY):
        d = {}
        for doc in (public, private):
            v = (doc.get(env) or {}).get(key) if isinstance(doc.get(env), dict) else None
            if isinstance(v, dict):
                d.update(v)
        out.append(d)
    return out[0], out[1]


def _local_port(meta: dict, env_values: dict) -> int:
    port = meta.get("port")
    if port is None:
        ports = {int(p) for v in env_values.values() for _, p in re.findall(LOCAL_PORT_RX % r"(\d{2,5})", str(v))}
        if len(ports) != 1:
            raise HttpError("the local port of the tunnel is not clear from the environment's values (%s) - fill in "
                            "the local port in the HTTP tab's Environment settings (or \"port\" in the env file's \"%s\")"
                            % (", ".join(str(p) for p in sorted(ports)) or "none on localhost", META_KEY))
        port = ports.pop()
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise HttpError('"%s": {"port": ...} must be a port number' % META_KEY)
    return port


def tunnel_spec(name: str, meta: dict, env_values: dict, from_name: bool) -> dict | None:
    """The tunnel Piper opens for runs of an environment, None when it declares none:
      "piper": {"tunnel": "test/<target>", "port"?: 8443}      a tunnel of the dev stack provider
      "piper": {"ssh": "user@host[:22]", "forward": "192.0.2.1:443", "port"?: 8443}   ssh -L
      or (from_name) the environment's name holds `ssh -L <port>:<host>:<port> [user@]server`.
    `port` is the local port the environment's URLs name (found by itself when they name exactly
    one on localhost / 127.0.0.1); Piper's own port replaces it in the run."""
    import dbtools
    if meta.get("tunnel"):
        m = re.match(r"^(test|prod)/([A-Za-z0-9_.-]{1,64})$", str(meta["tunnel"]))
        if not m:
            raise HttpError('"%s": {"tunnel": ...} must be "test/<target>" or "prod/<target>" (a tunnel target '
                            "of the dev stack)" % META_KEY)
        return {"kind": "stack", "env": m.group(1), "target": m.group(2), "port": _local_port(meta, env_values),
                "label": "stack tunnel %s/%s" % (m.group(1), m.group(2))}
    if meta.get("ssh"):
        m = re.match(r"^(?:([^@\s]+)@)?([^:@\s]+)(?::(\d{1,5}))?$", str(meta["ssh"]))
        f = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*):(\d{1,5})$", str(meta.get("forward") or ""))
        if not m or not f:
            raise HttpError('"%s": {"ssh": "user@server[:port]", "forward": "<host>:<port>"} - the forward is the '
                            "address as the SSH server sees it" % META_KEY)
        user, host, sport, fhost, fport, local = m.group(1), m.group(2), m.group(3), f.group(1), f.group(2), None
    elif from_name and meta.get("tunnel") is not False and SSH_NAME_RX.search(name):   # "tunnel": false = you open it
        g = SSH_NAME_RX.search(name)
        sport, local, fhost, fport, user, host = g.group(1) or g.group(5), int(g.group(2)), g.group(3), g.group(4), \
            g.group(6), g.group(7)
        # the name is only a note unless the values really go to that local port (and the server is a valid name)
        if meta.get("port") is None and not any(re.search(LOCAL_PORT_RX % local, str(v)) for v in env_values.values()):
            return None
        if not dbtools.HOST_RX.match(host) or not 1 <= int(fport) <= 65535:
            return None
    else:
        return None
    try:
        spec = dbtools.ssh_spec({"host": host, "user": user or "", "port": int(sport) if sport else 22})
    except dbtools.DbError as e:
        raise HttpError("the tunnel of %r: %s" % (name, e)) from None
    if not 1 <= int(fport) <= 65535:
        raise HttpError("the tunnel of %r: invalid port %s" % (name, fport))
    local = local if local and meta.get("port") is None else _local_port(meta, env_values)
    return {"kind": "ssh", "ssh": spec, "host": fhost, "rport": int(fport), "port": local,
            "label": "ssh %s -> %s:%s" % (dbtools.ssh_label({"ssh": spec}), fhost, fport)}


def port_rewrite(s: str, old: int, new: int) -> str:
    """localhost:<old> / 127.0.0.1:<old> -> the same host on <new> (Piper's own tunnel port)."""
    return re.sub(LOCAL_PORT_RX % old, lambda m: "%s:%d" % (m.group(1), new), s)


def ports_rewrite(s: str, ports: dict) -> str:
    """port_rewrite of several {old: new} ports in one pass: a new port equal to another old one is not
    rewritten a second time."""
    if not ports:
        return s
    return re.sub(LOCAL_PORT_RX % ("(%s)" % "|".join(str(p) for p in ports)),
                  lambda m: "%s:%d" % (m.group(1), ports[int(m.group(2))]), s)


def rewrite_env(doc: dict, env: str, old, new: int | None = None) -> dict:
    """A copy of an env file with the environment's string values pointed at Piper's tunnel port
    (old -> new, or old = {old: new} for several)."""
    ports = old if isinstance(old, dict) else {old: new}

    def walk(v):
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return ports_rewrite(v, ports) if isinstance(v, str) else v
    out = dict(doc)
    if isinstance(doc.get(env), dict):
        out[env] = walk(doc[env])
    return out


def is_text(ctype: str, body: bytes = b"") -> bool:
    """A response body a reader can look at (the rest is binary: a PDF, a zip, an image)."""
    if ctype:
        return bool(TEXT_CT_RX.match(ctype.strip()))
    return b"\0" not in body[:4096]


def hint_error(error: str, url: str, sandbox: str = "") -> str:
    """An ijhttp request error with what to do about it, when Piper knows. sandbox: the label of the
    session's sandbox the run went into."""
    if not error or " - hint: " in error:
        return error
    if sandbox and re.search(r"refused|reset|closed|end of (stream|file)|EOF|timed? ?out|terminated", error, re.I):
        return error + (" - hint: the request went into %s through Piper's relay - is the service still up there "
                        "and listening on that port?" % sandbox)
    if re.search(r"PKIX path|unable to find valid certification path|SSLHandshakeException|CERTIFICATE_VERIFY_FAILED"
                 r"|No subject alternative|certificate", error, re.I):
        return error + (" - hint: the server's certificate is self-signed or not for this name: add \"%s\": "
                        "{\"verifyHostCertificate\": false} to this environment in http-client/%s (IDEA honours it; "
                        "Piper only from an env file hidden from sessions - the user edits it)" % (SSL_KEY, ENV_FILE))
    h = url_host(url) or ""
    if h in ("localhost", "127.0.0.1") and re.search(r"Connection refused|ConnectException", error):
        return error + (" - hint: nothing listens there on the host (a tunnel opened by hand?): declare it in the "
                        "environment as \"%s\": {\"ssh\": \"user@server\", \"forward\": \"<host>:<port>\"} or "
                        "{\"tunnel\": \"test/<target>\"} in an env file hidden from sessions (http-client/%s) and "
                        "Piper opens it for every run" % (META_KEY, ENV_FILE))
    return error


def body_ref(body: str) -> tuple[str, bool] | None:
    """(reference, substituted) when the body is one `< file` / `<@ file` line, else None."""
    b = body.strip()
    m = REF_RX.match(b) if b and "\n" not in b else None
    return (m.group(1), bool(SUBST_REF_RX.match(b))) if m else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect comes back as the response (3xx + Location): following it would carry the request's
    headers - Authorization, session ids - to a host nobody checked."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):   # noqa: D102
        return None


def http_open(req: urllib.request.Request, timeout: int, insecure: bool):
    """urllib's open without env proxies and without redirects (tests replace it)."""
    ctx = ssl.create_default_context()
    if insecure:   # only where the user opted in: http.insecure, SSLConfiguration, a tunnel Piper opened
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx),
                                         _NoRedirect())
    return opener.open(req, timeout=timeout)   # noqa: S310 - a URL of the project's .http file, host-checked


def fetch_request(r: dict, values: dict, mapping: dict, insecure: bool, timeout: int) -> tuple[dict, bytes]:
    """One request of a parsed file sent by piperd itself (urllib) with the environment's values: the
    result row (like parse_console's) and the raw body. For save_to: ijhttp's console cannot carry a
    binary body. The response handler and its tests do not run; no `{{$dynamic}}` variables."""
    url = substitute(r["url"], values)
    headers = [(k, substitute(v, values)) for k, v in r["headers"]]
    row = {"name": r["name"], "method": r["method"], "url": url, "req_headers": [[k, v] for k, v in headers],
           "status": None, "status_text": "", "headers": [], "body": "", "time_ms": None, "error": "",
           "tests": [], "globals": {}, "ok": False}
    if r["pre"] or [x for x in r["scripts"] if x != r["handler_file"]]:
        row["error"] = "a request with a pre-request script cannot be saved to a file - run it without save_to"
        return row, b""
    data, ref = None, body_ref(r["body"])
    if ref:
        path = mapping.get(ref[0])
        if not path:
            row["error"] = "%s: the referenced body file was not snapshotted" % ref[0]
            return row, b""
        with open(path, "rb") as f:
            data = f.read()
        if ref[1]:
            data = substitute(data.decode("utf-8", "replace"), values).encode()
    elif r["body"]:
        if re.search(r"^\s*<@?\s", r["body"], re.M):
            row["error"] = "a body mixing text and `< file` lines cannot be saved to a file - run it without save_to"
            return row, b""
        data = substitute(r["body"], values).encode()
    unresolved = sorted({m.group(1) for x in [url] + [v for _, v in headers] + [(data or b"").decode("utf-8", "replace")]
                         for m in VAR_USE_RX.finditer(x)})
    if unresolved:
        row["error"] = ("unresolved %s - a request saved to a file knows the environment's values, the file's "
                        "variables and the globals of the last run, no {{$dynamic}} ones"
                        % ", ".join("{{%s}}" % u for u in unresolved))
        return row, b""
    # ijhttp accepts `Basic user password`; urllib needs the wire-format Base64 token.
    # Convert only the two-credential shorthand, after unresolved-variable validation:
    # an already encoded token and other authorization schemes must stay unchanged.
    wire_headers = []
    for key, value in headers:
        basic = re.fullmatch(r"Basic[ \t]+([^ \t]+)[ \t]+(.+)", value, re.I) if key.lower() == "authorization" else None
        if basic:
            value = "Basic " + base64.b64encode((basic[1] + ":" + basic[2]).encode("utf-8")).decode("ascii")
        wire_headers.append((key, value))
    row["req_headers"] = [[k, v] for k, v in wire_headers]
    try:
        req = urllib.request.Request(url, data=data, method=r["method"], headers=dict(wire_headers))
    except ValueError as e:
        row["error"] = "invalid request: %s" % e
        return row, b""
    started = time.time()
    try:
        with http_open(req, timeout, insecure) as resp:
            status, reason, hdrs = resp.status, resp.reason, list(resp.headers.items())
            chunks, size = [], 0   # `timeout` is per socket read: the whole download gets it as a deadline too
            while size <= MAX_SAVE:
                if time.time() - started > timeout:
                    row["error"] = "the response took longer than %d s - not saved" % timeout
                    return row, b""
                c = resp.read(1 << 16)
                if not c:
                    break
                chunks.append(c)
                size += len(c)
            raw = b"".join(chunks)
    except urllib.error.HTTPError as e:   # 3xx (redirects are not followed), 4xx, 5xx: still a response
        with e:
            raw = e.read(MAX_SAVE + 1) if e.fp else b""
        status, reason, hdrs = e.code, e.reason, list(e.headers.items()) if e.headers else []
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        why = getattr(e, "reason", e)
        row["error"] = hint_error("%s: %s" % (type(why).__name__, why), url)
        return row, b""
    row.update(status=int(status), status_text=str(reason or ""), headers=[[k, v] for k, v in hdrs],
               time_ms=int((time.time() - started) * 1000))
    if len(raw) > MAX_SAVE:
        row["error"] = "the response is larger than %d MB - not saved" % (MAX_SAVE >> 20)
        return row, b""
    ctype = next((v for k, v in hdrs if k.lower() == "content-type"), "")
    row["body"] = raw.decode("utf-8", "replace") if is_text(ctype, raw) else ""
    row["ok"] = 200 <= row["status"] < 300
    if not row["ok"]:
        row["error"] = "HTTP %d %s - not saved" % (row["status"], row["status_text"])
    return row, raw


def fetch_deadline(r: dict, values: dict, mapping: dict, insecure: bool, timeout: int) -> tuple[dict, bytes]:
    """fetch_request with a hard wall-clock deadline: urllib's timeout is per socket operation (headers sent
    after 230 s, a body trickling in), and a session's run must end inside piper-mcp's 900 s - a late answer
    makes the agent call again and the request happen twice. A fetch past the deadline is abandoned (its
    response is never saved)."""
    out: dict = {}

    def work():
        try:
            out["r"] = fetch_request(r, values, mapping, insecure, timeout)
        except Exception as e:  # noqa: BLE001 - reported as the request's error
            out["e"] = e
    t = threading.Thread(target=work, daemon=True, name="piper-http-save")
    t.start()
    t.join(timeout + 5)
    if "r" in out:
        return out["r"]
    row = {"name": r["name"], "method": r["method"], "url": substitute(r["url"], values), "req_headers": [],
           "status": None, "status_text": "", "headers": [], "body": "", "time_ms": None, "tests": [], "globals": {},
           "ok": False, "error": ("%s: %s" % (type(out["e"]).__name__, out["e"])) if "e" in out
           else "no complete response within %d s - not saved" % timeout}
    return row, b""


def tunnel_probe(port: int, timeout: float = 5) -> str:
    """Does anything answer BEHIND a local tunnel port (ssh -L accepts locally even when its forward goes
    nowhere, and closes right after)? A TLS handshake first (an https server), else a plain `HEAD /`.
    "answers" | "closed" (accepted, then closed at once) | "silent" (no reply in time) | "down" (nothing listens)."""
    import socket
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # a probe only: nothing is sent or read but the handshake
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname="localhost"):
                return "answers"
    except (ssl.SSLEOFError, ConnectionResetError, BrokenPipeError):
        pass   # closed during the handshake: maybe plain HTTP, maybe nothing behind
    except ssl.SSLError:
        return "answers"   # bytes came back, just not TLS: a plain server answered
    except socket.timeout:
        return "silent"
    except OSError:
        return "down"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as so:
            so.sendall(b"HEAD / HTTP/1.0\r\n\r\n")
            return "answers" if so.recv(1) else "closed"
    except socket.timeout:
        return "silent"
    except OSError:
        return "down"


# ------------------------------------------------------------------ a session's own sandbox
LOCAL_HOSTS = ("localhost", "127.0.0.1")
# run inside the agent container (python3 is in every sandbox image - piper-mcp.py runs there):
# argv <host> <port> [probe]; probe = connect and exit 0 / 1, else stdin/stdout <-> one TCP connection
RELAY_SCRIPT = r"""
import os, socket, sys, threading
try:
    s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5)
except OSError:
    sys.exit(1)
if len(sys.argv) > 3:
    sys.exit(0)
s.settimeout(None)
def up():
    while True:
        b = sys.stdin.buffer.read1(65536)
        if not b:
            break
        s.sendall(b)
    try:
        s.shutdown(socket.SHUT_WR)
    except OSError:
        pass
threading.Thread(target=up, daemon=True).start()
while True:
    b = s.recv(65536)
    if not b:
        break
    sys.stdout.buffer.write(b)
    sys.stdout.buffer.flush()
os._exit(0)   # at once: a normal exit can wait on the stdin thread, and the exec would live until ijhttp lets go
"""
SCRIPT_SET_RX = re.compile(r"(?:client\.global|request\.variables)\.set\(\s*([^,)]*)")


def local_ports(urls: list[str]) -> dict | None:
    """{port: host} of request URLs that all go to localhost / 127.0.0.1 with an explicit port, else None."""
    out = {}
    for u in urls:
        h = url_host(u)
        try:
            sp = urllib.parse.urlsplit(u if "://" in u else "http://" + u)
            port = sp.port
        except ValueError:
            return None
        if sp.scheme not in ("http", "https") or h not in LOCAL_HOSTS or not port or out.get(port, h) != h:
            return None
        out[port] = h
    return out or None


def url_var_names(urls: list[str], values: dict) -> set[str]:
    """{{names}} the URLs use, also through the values they resolve to."""
    names, todo = set(), list(urls)
    while todo:
        for m in VAR_USE_RX.finditer(todo.pop()):
            n = m.group(1)
            if n not in names:
                names.add(n)
                if isinstance(values.get(n), str):
                    todo.append(values[n])
    return names


def url_head(url: str) -> tuple[str, str]:
    """(the scheme and authority part of a request URL as written, the rest): cut at the first `/`, `?` or `#`
    outside `{{…}}` after an optional `scheme://` - `{{base}}/a/{{id}}` -> (`{{base}}`, `/a/{{id}}`)."""
    m = re.match(r"(?:[a-z0-9+.-]|\{\{[^{}]*\}\})+://", url, re.I)   # also `{{scheme}}://`
    i = m.end() if m else 0
    while i < len(url):
        if url.startswith("{{", i):
            j = url.find("}}", i)
            i = len(url) if j < 0 else j + 2
        elif url[i] in "/?#":
            break
        else:
            i += 1
    return url[:i], url[i:]


def pin_url(url: str, values: dict) -> str:
    """A request URL written out for the sandbox route: the scheme, the host and port Piper checked (Piper's
    values substituted, lower case, no userinfo) - nothing ijhttp parses another way (`LOCALHOST`, a port in a
    variable) and no variable a script sets at run time can move it. The path and query after it stay as
    written: ijhttp substitutes their variables at run time (`/users/{{id}}` with an id request 1 set)."""
    head, rest = url_head(url)
    full = substitute(head, values)
    p = urllib.parse.urlsplit(full if "://" in full else "http://" + full)
    if "{{" in p.netloc or not p.hostname or not p.port:
        raise HttpError("the host of %s is not known before the run - a run into the session's sandbox fixes "
                        "every host before it starts" % url[:200])
    tail = p.path + ("?" + p.query if p.query else "") + ("#" + p.fragment if p.fragment else "") + rest
    if not tail.startswith("/"):
        tail = "/" + tail
    return "%s://%s:%d%s" % (p.scheme, p.hostname, p.port, tail)


def pin_rows(rows: list, text: str, requests: list[dict], values: dict) -> tuple[list, dict]:
    """(text, original line) rows with each request line written out by pin_url (its `?`/`&` continuation
    lines folded in), and {request line: pinned URL}."""
    lines, pins, drop = text.split("\n"), {}, set()
    for r in requests:
        pins[r["line"]] = pin_url(r["url"], values)
        i = r["line"] + 1
        while i < len(lines) and lines[i][:1] in (" ", "\t") and lines[i].strip()[:1] in ("?", "&"):
            drop.add(i)
            i += 1
    out, done = [], set()
    for t, o in rows:
        if o in drop:
            continue
        if o in pins and t == lines[o]:   # not single_rows' `### name` row that may share the number
            ver = re.search(r"\s(HTTP/[\d.]+)\s*$", t)
            r = next(x for x in requests if x["line"] == o)
            t = "%s %s%s" % (r["method"], pins[o], " " + ver.group(1) if ver else "")
            done.add(o)
        out.append((t, o))
    if done != set(pins):   # what guarantees the host: every selected request line is the written-out one
        raise HttpError("Piper could not write out every request URL for a run into the session's sandbox")
    return out, pins


def no_redirect_rows(rows: list, requests: list[dict]) -> list:
    """(text, original line) rows with `# @no-redirect` at the top of each request's block (under its `###`):
    a server in the sandbox must not send the host's ijhttp anywhere else. Not above the request line -
    ijhttp ignores the tag below a pre-request script (inline or `< ./x.js`) and follows (verified, 2026.1)."""
    starts, out = {r["start"] for r in requests}, []
    for t, o in rows:
        if o in starts and not t.startswith("###"):
            out.append(("# @no-redirect", None))
        out.append((t, o))
        if o in starts:
            if t.startswith("###"):
                out.append(("# @no-redirect", None))
            starts.discard(o)   # single_rows' `### name` row shares its number with the block's first line
    return out


RELAY_HEAD_MAX = 64 << 10        # a header block the relay looks at; a longer one passes through untouched
RELAY_TEXT_MAX = 8 << 20         # a binary body up to this size is relayed as text/plain (VERBOSE prints it whole)
RELAY_CT = "X-Piper-Content-Type"   # the original Content-Type of a body the relay turned into text/plain


def relay_request_head(head: bytes) -> bytes:
    """A request's header block with `Connection: close`: one response per relayed connection, so the
    relay only ever has to read the first response head (ijhttp 2026.1 opens a connection per request anyway)."""
    lines = head[:-4].split(b"\r\n")
    keep = [ln for ln in lines[1:] if ln.split(b":", 1)[0].strip().lower() != b"connection"]
    return b"\r\n".join([lines[0]] + keep + [b"Connection: close"]) + b"\r\n\r\n"


def relay_response_head(head: bytes) -> bytes:
    """A response's header block with a binary Content-Type (not TEXT_CT_RX) turned into text/plain and the
    original in X-Piper-Content-Type: ijhttp 2026.1 cancels a run on a binary body (most runs, at random -
    verified 1/5 with image/png, 5/5 with the same bytes as text/plain). The body itself is not touched."""
    lines = head[:-4].split(b"\r\n")
    out, ct, clen = [lines[0]], "", None
    for ln in lines[1:]:
        name, _, value = ln.partition(b":")
        name = name.strip().lower()
        if name in (RELAY_CT.lower().encode(), b"connection", b"keep-alive"):
            continue   # only the relay sets it: the record trusts it / closed below
        if name == b"content-type":
            ct = value.strip().decode("latin-1")
        elif name == b"content-length":
            try:
                clen = int(value.strip())
            except ValueError:
                pass
        out.append(ln)
    if ct and not is_text(ct) and (clen is None or clen <= RELAY_TEXT_MAX):
        out = [ln if ln.split(b":", 1)[0].strip().lower() != b"content-type" else b"Content-Type: text/plain"
               for ln in out] + [("%s: %s" % (RELAY_CT, ct)).encode("latin-1")]
    # ijhttp 2026.1 opens a connection per request and keeps the old ones open: each held a `docker exec` (and its
    # process in the container) until the run ended - ~70 requests in, the sandbox refused more (Connection reset)
    return b"\r\n".join(out + [b"Connection: close"]) + b"\r\n\r\n"


class RelayHeadFilter:
    """The first HTTP/1.x header block of one direction of a relayed connection goes through `rewrite`, the rest
    passes raw. In the response direction 1xx heads (100 Continue) pass first and the next head is rewritten.
    Anything unexpected (no `HTTP/1.` / request line, a head over RELAY_HEAD_MAX, 101) -> raw from there on."""

    def __init__(self, rewrite, response: bool):
        self.rewrite, self.response, self.buf, self.raw = rewrite, response, b"", False

    def feed(self, b: bytes) -> bytes:
        if self.raw:
            return b
        self.buf += b
        out = b""
        # what cannot become a start line (a TLS handshake of https://localhost) passes at once: it holds no
        # blank line and would wait here for a head that never comes
        if not (b"HTTP/1."[:len(self.buf)] == self.buf[:7] if self.response else self.buf[:1].isupper()):
            out, self.buf, self.raw = self.buf, b"", True
        while not self.raw:
            i = self.buf.find(b"\r\n\r\n")
            if i < 0:
                if len(self.buf) > RELAY_HEAD_MAX:
                    out, self.buf, self.raw = out + self.buf, b"", True
                break
            head, rest = self.buf[:i + 4], self.buf[i + 4:]
            if self.response:
                m = re.match(rb"HTTP/1\.[01] (\d{3})", head)
                if not m:
                    out, self.buf, self.raw = out + self.buf, b"", True
                    break
                code = int(m.group(1))
                if 100 <= code < 200 and code != 101:
                    out, self.buf = out + head, rest
                    continue
                if code == 101:
                    out, self.buf, self.raw = out + self.buf, b"", True
                    break
            elif not re.match(rb"[A-Z]+ \S+ HTTP/1\.[01]\r\n", head):
                out, self.buf, self.raw = out + self.buf, b"", True
                break
            out, self.buf, self.raw = out + self.rewrite(head) + rest, b"", True
        return out

    def flush(self) -> bytes:
        out, self.buf = self.buf, b""
        return out


def unrelay_headers(r: dict) -> bool:
    """The Content-Type the relay turned into text/plain back in a result's headers (True when it did)."""
    orig = next((v for k, v in r["headers"] if k.lower() == RELAY_CT.lower()), None)
    if orig is None:
        return False
    r["headers"] = [[k, orig if k.lower() == "content-type" else v] for k, v in r["headers"]
                    if k.lower() != RELAY_CT.lower()]
    return True


def unrelay_binary(r: dict) -> None:
    """A result whose body the relay turned into text/plain: its Content-Type back, no body (the text is the
    binary bytes decoded), `binary` as for any binary response - the GUI and the agent's view show the size."""
    if not unrelay_headers(r):
        return
    clen = next((v for k, v in r["headers"] if k.lower() == "content-length"), "")
    r["body_size"] = int(clen) if clen.isdigit() else len(r["body"].encode("utf-8", "replace"))
    r["body"], r["binary"] = "", True


class SandboxRelay:
    """127.0.0.1:<port> on the host -> one target inside a session's agent container, for one run: each
    connection is its own `docker exec -i … python3 RELAY_SCRIPT <host> <port>` (argv given whole)."""

    def __init__(self, argv: list[str]):
        import socket
        self.argv, self.procs, self.closed = argv, [], False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.5)   # close() is seen within half a second (a closed socket may not wake accept)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True, name="http-relay").start()

    def _serve(self) -> None:
        while not self.closed:
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.settimeout(None)
            threading.Thread(target=self._pipe, args=(conn,), daemon=True).start()

    def _pipe(self, conn) -> None:
        import socket
        try:
            p = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            conn.close()
            return
        self.procs.append(p)
        if self.closed:   # accepted just before close(): close() may have looked at procs already
            p.kill()

        req, resp = RelayHeadFilter(relay_request_head, False), RelayHeadFilter(relay_response_head, True)

        def up():
            try:
                while True:
                    b = conn.recv(65536)
                    if not b:
                        break
                    p.stdin.write(req.feed(b))
                    p.stdin.flush()
                p.stdin.write(req.flush())
            except OSError:
                pass
            try:
                p.stdin.close()
            except OSError:
                pass
        threading.Thread(target=up, daemon=True).start()
        try:
            while True:
                b = os.read(p.stdout.fileno(), 65536)
                if not b:
                    conn.sendall(resp.flush())
                    break
                conn.sendall(resp.feed(b))
        except OSError:
            pass
        finally:
            # Linux keeps a socket alive while up() blocks in recv(): close() alone neither wakes that
            # thread nor sends EOF to the client. Shutdown both directions before releasing the descriptor.
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
            p.kill()
            p.wait()
            p.stdout.close()

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        for p in self.procs:
            if p.poll() is None:
                p.kill()


def secret_hits(raw: bytes, secret_list: list[str]) -> bool:
    """Does a raw body hold a secret as such, base64- or URL-encoded (what the redactor masks in text)?"""
    for s in secret_list:
        b = s.encode()
        for v in (b, urllib.parse.quote(s, safe="").encode(), urllib.parse.quote_plus(s).encode(),
                  base64.b64encode(b).rstrip(b"="), base64.urlsafe_b64encode(b).rstrip(b"=")):
            if len(v) >= 4 and v in raw:
                return True
    text = raw.decode("latin-1")   # a base64 token that decodes to something holding a secret (Basic user:pass)
    for m in B64_TOKEN_RX.finditer(text):
        tok = m.group(0)
        try:
            dec = base64.b64decode(tok.replace("-", "+").replace("_", "/") + "=" * (-len(tok) % 4)).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            continue
        if any(x in dec for x in secret_list):
            return True
    return False


# ------------------------------------------------------------------ ijhttp output
def parse_console(out: str) -> list[dict]:
    """Requests and responses of an `ijhttp -L VERBOSE` run, in order."""
    reqs, cur, mode, buf = [], None, None, []
    for ln in out.split("\n"):
        if mode != "body" and ln.startswith("Request '"):
            m = re.match(r"^Request '(.*)' (\S+) (.*)$", ln)
            cur = {"name": m.group(1) if m else ln[9:], "method": m.group(2) if m else "", "url": m.group(3) if m else "",
                   "req_headers": [], "status": None, "status_text": "", "headers": [], "body": "", "time_ms": None,
                   "error": ""}
            reqs.append(cur)
            mode = None
            continue
        if cur is None:
            continue
        if ln.startswith("= request =>"):
            mode, buf = "req", []
        elif ln.startswith("<= response ="):
            mode, buf = "resp_status", []
        elif mode == "req":
            if ln.strip() == "###" or not ln.strip():
                mode = "req_body"
            elif ":" in ln and not ln.startswith(cur["method"] + " "):
                k, v = ln.split(":", 1)
                cur["req_headers"].append([k.strip(), v.strip()])
        elif mode == "resp_status":
            m = re.match(r"^HTTP/\S+ (\d{3}) ?(.*)$", ln)
            if m:
                cur["status"], cur["status_text"], mode = int(m.group(1)), m.group(2), "resp_headers"
        elif mode == "resp_headers":
            if not ln.strip():
                mode, buf = "body", []
            elif ":" in ln:
                k, v = ln.split(":", 1)
                cur["headers"].append([k.strip(), v.strip()])
        elif mode == "body" and ln.startswith(BINARY_MARK):   # no "Response code" line follows (CLI 2026.1)
            cur["body"], cur["binary"], mode = "", True, None
        elif mode == "body":
            m = re.match(r"^Response code: (\d+).*?Time: (\d+)ms", ln)
            if m:
                while buf and not buf[-1].strip():
                    buf.pop()
                cur["body"], cur["time_ms"], mode = "\n".join(buf), int(m.group(2)), None
            else:
                buf.append(ln)
        elif ln.startswith("Unable to get response for"):
            mode = "error"
        elif mode == "error" and ln.strip() and not cur["error"]:
            cur["error"] = ln.strip()[:500]
    return reqs


def parse_junit(xml: str) -> list[dict]:
    """Per request (Pre-request Handler, if present, then Response): tests [{name, ok, message}],
    error (the request itself failed), globals (carried by the CAPTURE test)."""
    out = []
    if re.search(r"<!(DOCTYPE|ENTITY)", xml):   # ijhttp never writes one: no entity expansion
        return out
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    pre_request = False
    cases = list(root.iter("testcase"))
    for i, tc in enumerate(cases):
        name = tc.get("name") or ""
        # A user's client.test may have this name too; only the engine's pre-handler is followed
        # immediately by Response for the same request. Classnames alone can repeat across requests.
        pre_handler = (name == "Pre-request Handler" and i + 1 < len(cases) and
                       cases[i + 1].get("name") == "Response" and
                       cases[i + 1].get("classname") == tc.get("classname"))
        fail = tc.find("failure")
        if fail is None:
            fail = tc.find("error")
        msg = (fail.get("message") or fail.text or "").strip() if fail is not None else ""
        if pre_handler or (name == "Response" and not pre_request) or not out:
            out.append({"tests": [], "error": "", "globals": {}})
        if pre_handler or name == "Response":
            pre_request = pre_handler
        cur = out[-1]
        if name == "Response":
            cur["error"] = msg
        elif name.startswith(CAPTURE):
            try:
                g = json.loads(name[len(CAPTURE):])
                cur["globals"] = {k: v for k, v in g.items() if v is not None} if isinstance(g, dict) else {}
            except ValueError:
                pass
        elif pre_handler or name == "Response Handler":
            if fail is not None:
                cur["tests"].append({"name": name, "ok": False, "message": msg[:2000]})
        else:
            cur["tests"].append({"name": name, "ok": fail is None, "message": msg[:2000]})
    return out


def merge(console: list[dict], junit: list[dict]) -> list[dict]:
    out = []
    for i, c in enumerate(console):
        j = junit[i] if i < len(junit) else {"tests": [], "error": "", "globals": {}}
        r = dict(c, tests=j["tests"], globals=j["globals"])
        if j["error"] and not r["error"]:
            r["error"] = j["error"].split("\n")[0][:500]
        r["ok"] = not r["error"] and all(t["ok"] for t in r["tests"])
        out.append(r)
    return out


# ------------------------------------------------------------------ the runner (host)
def find_ijhttp(policy_http: dict) -> str | None:
    for c in (os.environ.get("PIPER_IJHTTP"), policy_http.get("ijhttp"),
              os.path.join(common.PIPER_HOME, "ijhttp", "ijhttp"), shutil.which("ijhttp")):
        if c and os.path.isfile(os.path.expanduser(c)) and os.access(os.path.expanduser(c), os.X_OK):
            return os.path.expanduser(c)
    return None


_JAVA: dict = {}


def java_env() -> dict:
    """piperd's environment plus JAVA_HOME when it has none (a launchd service usually has none)."""
    env = dict(os.environ)
    if not env.get("JAVA_HOME") and os.path.exists("/usr/libexec/java_home"):
        if "home" not in _JAVA:
            try:
                r = subprocess.run(["/usr/libexec/java_home"], capture_output=True, timeout=10, text=True)
                _JAVA["home"] = r.stdout.strip() if r.returncode == 0 else ""
            except (OSError, subprocess.TimeoutExpired):
                _JAVA["home"] = ""
        if _JAVA["home"]:
            env["JAVA_HOME"] = _JAVA["home"]
    env.pop("JAVA_TOOL_OPTIONS", None)
    return env


def java_missing(env: dict) -> bool:
    """No JAVA_HOME and no java on PATH: ijhttp's launcher cannot start (a fresh Linux / WSL)."""
    return not env.get("JAVA_HOME") and not shutil.which("java", path=env.get("PATH"))


def run_ijhttp(binary: str, workdir: str, http_file: str, env_name: str, env_dir: str | None,
               variables: dict, insecure: bool, timeout: int) -> tuple[int, str, str]:
    """(rc, console, junit xml) of one run; the console is stdout+stderr."""
    report = os.path.join(workdir, "report")
    argv = [binary, "--no-progress", "-L", "VERBOSE", "-e", env_name, "-r", report,
            "--connect-timeout", "5000", "--socket-timeout", "60000"]
    if env_dir:
        for flag, name in (("-v", ENV_FILE), ("-p", PRIVATE_FILE)):
            if os.path.isfile(os.path.join(env_dir, name)):
                argv += [flag, os.path.join(env_dir, name)]
    if insecure:
        argv.append("--insecure")
    for k, v in variables.items():
        argv += ["-V", "%s=%s" % (k, v)]
    argv.append(http_file)
    env = java_env()
    try:
        r = subprocess.run(argv, cwd=workdir, capture_output=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise HttpError("ijhttp did not finish in %d s" % timeout) from None
    except OSError as e:
        raise HttpError("ijhttp could not start: %s" % e) from None
    console = (r.stdout + b"\n" + r.stderr).decode("utf-8", "replace")
    xml = ""
    rp = os.path.join(report, "report.xml")
    # the launcher never got to run (a failed request exits non-zero too, with a report; a test's fake needs no Java)
    if r.returncode and not os.path.isfile(rp) and java_missing(env):
        raise HttpError("ijhttp needs Java 17 or newer and there is none on piperd's PATH (Linux / WSL: "
                        "sudo apt install openjdk-17-jre-headless, then task piper:install-service)")
    if os.path.isfile(rp):
        with open(rp, encoding="utf-8", errors="replace") as f:
            xml = f.read()
    return r.returncode, console, xml


SAVE_HIDDEN_MAX = 64 << 20   # the old content of a hidden save_to target goes to the redo journal


class HttpRunner:
    """Manager mixin: .http runs for the GUI and for the session tools."""

    def _http_init(self) -> None:
        self._http_locks: dict = {}           # project key -> lock (one run per project at a time)
        self._http_locks_lock = threading.Lock()
        self._http_session_ok: set = set()    # (session id, env dir, env) allowed "for this session" (test)
        self._http_install = {"state": "", "error": ""}

    # ------------------------------------------------------------ settings and paths
    def _http_policy(self) -> dict:
        return dict(DEFAULTS, **(self.policy.get("http") or {}))

    def http_status(self) -> dict:
        b = find_ijhttp(self._http_policy())
        return {"ijhttp": b, "installing": self._http_install["state"] == "running",
                "install_error": self._http_install["error"]}

    def _http_lock(self, key: str) -> threading.Lock:
        with self._http_locks_lock:
            return self._http_locks.setdefault(key, threading.Lock())

    @staticmethod
    def _http_read(root: str, rel: str) -> str:
        if not rel.endswith(".http") and not rel.endswith(".rest"):
            raise HttpError("not a .http file: %s" % rel)
        try:
            return common.safe_read(root, rel, MAX_FILE + 1).decode("utf-8", "replace")
        except (OSError, ValueError) as e:
            raise HttpError("cannot read %s: %s" % (rel, e)) from None

    @staticmethod
    def _env_dirs(root: str) -> list[str]:
        """Project-relative directories holding an env file (no symlinks followed)."""
        out = []
        for d, dirs, files in os.walk(root):
            rel = os.path.relpath(d, root)
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not os.path.islink(os.path.join(d, x))]
            if rel.count(os.sep) >= 6:
                dirs[:] = []
            if ENV_FILE in files or PRIVATE_FILE in files:
                out.append("" if rel == "." else rel.replace(os.sep, "/"))
        return sorted(out, key=lambda r: (r.count("/"), r))

    def _env_dir_for(self, root: str, rel: str, dirs: list[str]) -> str | None:
        """The env directory of a file: the nearest one above it, else a project's only one,
        else <root>/http-client."""
        parts = rel.split("/")[:-1]
        for k in range(len(parts), -1, -1):
            cand = "/".join(parts[:k])
            if cand in dirs:
                return cand
        if len(dirs) == 1:
            return dirs[0]
        return "http-client" if "http-client" in dirs else (dirs[0] if dirs else None)

    @staticmethod
    def _load_env(root: str, env_dir: str | None) -> tuple[dict, dict]:
        out = []
        for name in (ENV_FILE, PRIVATE_FILE):
            if env_dir is None:
                out.append({})
                continue
            rel = (env_dir + "/" if env_dir else "") + name
            try:
                doc = json.loads(common.safe_read(root, rel, MAX_FILE).decode("utf-8") or "{}")
            except FileNotFoundError:
                doc = {}
            except (OSError, ValueError) as e:
                raise HttpError("cannot read %s: %s" % (rel, e)) from None
            out.append(doc if isinstance(doc, dict) else {})
        return out[0], out[1]

    def _env_list(self, key: str, public: dict, private: dict) -> list[dict]:
        over = (self._http_policy().get("classes") or {}).get(key) or {}
        names = [n for n in list(public) + [k for k in private if k not in public] if not n.startswith("$")]
        return [{"name": n, "cls": classify(n, over.get(n))} for n in names]

    def _env_class(self, key: str, env: str) -> str:
        return classify(env, ((self._http_policy().get("classes") or {}).get(key) or {}).get(env))

    # ------------------------------------------------------------ contexts
    def _http_gui_ctx(self, key: str) -> dict:
        p = self.gui_project(key)
        root = p["path"]
        top = self.repo_top(root)
        return {"key": p["key"], "root": root, "hidden": self.project_hidden(p), "top": top, "by": "gui"}

    def _http_session_ctx(self, s) -> dict:
        if s.kind != "project" or not s.path:
            raise HttpError("only a project session runs .http files")
        top = self._session_top(s)
        hf = self.hidden_for(s)
        root = s.path
        real = os.path.realpath(root)

        def hidden(rel: str) -> bool:   # hidden_for takes repository-relative paths
            return True if top is None else hf(os.path.relpath(os.path.join(real, rel), top))
        return {"key": s.project, "root": root, "hidden": hidden, "top": top, "by": s.id, "session": s}

    @staticmethod
    def _norm(rel: str) -> str:
        rel = str(rel or "").strip().lstrip("/")
        parts = [x for x in rel.replace("\\", "/").split("/") if x not in ("", ".")]
        if not parts or ".." in parts:
            raise HttpError("a path inside the project is required")
        return "/".join(parts)

    def _changed(self, ctx: dict, rels: list[str]) -> bool:
        """Does any of these project-relative files differ from git HEAD (or is untracked)?"""
        top = ctx["top"]
        if not top:
            return True
        real = os.path.realpath(ctx["root"])
        for rel in rels:
            r = os.path.relpath(os.path.join(real, rel), top)
            try:
                if not gitops.is_tracked(top, r):
                    return True
                gitops._git(top, "diff", "--quiet", "HEAD", "--", gitops.literal(r), session=True)
            except gitops.GitError:
                return True
        return False

    # ------------------------------------------------------------ what a file offers
    def http_envs(self, ctx: dict, rel: str) -> dict:
        rel = self._norm(rel)
        text = self._http_read(ctx["root"], rel)
        dirs = self._env_dirs(ctx["root"])
        env_dir = self._env_dir_for(ctx["root"], rel, dirs)
        public, private = self._load_env(ctx["root"], env_dir)
        parsed = parse(text)
        urls = {}   # the request URLs per environment, resolved as far as known before a run - the GUI's only (L3c:
        # its own values, unmasked; the session's http_envs drops `urls`)
        for e in self._env_list(ctx["key"], public, private):
            pub, priv = public.get(e["name"]) or {}, private.get(e["name"]) or {}
            vals = dict(flatten(pub), **flatten(priv))
            vals.update({v["name"]: substitute(v["value"], vals) for v in parsed["vars"]})
            urls[e["name"]] = [substitute(r["url"], vals) for r in parsed["requests"]]
        tunnels = {}   # the tunnel Piper opens for an environment's runs (or why it cannot)
        for e in self._env_list(ctx["key"], public, private):
            try:
                t = self._http_tunnel(ctx, e["name"], env_dir, public, private, e["cls"])
            except HttpError as x:
                tunnels[e["name"]] = "error: %s" % x
            else:
                if t:
                    tunnels[e["name"]] = t["label"]
        return {"file": rel, "sha": hashlib.sha256(text.encode()).hexdigest(), "env_dir": env_dir, "env_dirs": dirs, "urls": urls,
                "tunnels": tunnels,
                "envs": self._env_list(ctx["key"], public, private),
                "variables": {e: sorted(set(flatten(public.get(e) or {})) | set(flatten(private.get(e) or {})))
                              for e in list(public) + list(private) if not e.startswith("$")},
                "requests": requests_brief(parsed), **self.http_status()}

    # ------------------------------------------------------------ one run
    def _refs(self, ctx: dict, rel: str, parsed: dict, agent: bool) -> dict:
        """Referenced files (`< x`, `> x.js`): project-relative path per reference as written."""
        base = os.path.dirname(rel)
        out = {}
        for r in parsed["requests"]:
            if r["saves"]:   # `>> file`: the CLI (2026.1) saves nothing and hangs until the timeout (verified)
                raise HttpError("request %s: `>> %s` (saving the response to a file) is not supported here - "
                                "the ijhttp CLI hangs on it; remove the line and run the request with index + "
                                "save_to instead" % (r["name"], r["saves"][0]))
            for ref in r["files"]:
                if ref in out:
                    continue
                if os.path.isabs(ref) or ref.startswith("~"):
                    raise HttpError("%s: a referenced file must be relative to the .http file" % ref)
                try:
                    p = self._norm(os.path.normpath(os.path.join(base, ref)))
                except HttpError:
                    raise HttpError("%s: a referenced file must stay inside the project" % ref) from None
                if agent and ctx["hidden"](p):
                    raise HttpError("%s is hidden by the isolation policy - a session cannot send it" % ref)
                out[ref] = p
        return out

    def _agent_checks(self, ctx: dict, text: str, sel: list, public: dict, private: dict, env: str,
                      env_dir: str | None, refs: dict, values: dict) -> tuple[bool, list[str], list[str]]:
        """(file changed vs HEAD, warnings, hosts outside the environment) of a session's run;
        HttpError when it is refused. Every run is checked - never "committed = trusted": git is
        open in the sandbox, so the agent can commit what it wrote."""
        changed = self._changed(ctx, [ctx["rel"]] + sorted(set(refs.values())))
        # hosts: the environment's values - only from env files the sandbox cannot see or write when
        # there is one (a visible, writable env file could add a host of the agent's choice) - and the
        # hosts the user allowed for this project on an http_host card
        # (env files the sandbox can see may be its own: a planted env dir next to its .http file -
        # their hosts are never trusted, each one needs the http_host card once per project)
        files = [(n, d) for n, d in ((ENV_FILE, public), (PRIVATE_FILE, private)) if d]
        trusted = [d for n, d in files if ctx["hidden"]((env_dir + "/" if env_dir else "") + n)]
        allowed = set((read_json(HOSTS_FILE, {}).get(ctx["key"]) or []))
        for d in trusted:
            for v in flatten(d.get(env) or {}).values():
                allowed |= hosts_in(str(v))
        foreign = []
        for r in sel:
            h = url_host(substitute(r["url"], values))
            if h is None:
                raise HttpError("request %s: the host of %s is not known before the run - a session's run may "
                                "only call hosts known in advance (environment values, hosts you allowed)"
                                % (r["name"], r["url"]))
            if h not in allowed and h not in foreign:
                foreign.append(h)
        warns = []
        if re.search(r"(request\.environment|client\.environment|request\.variables)", text) or any(
                re.search(r"(request\.environment|client\.environment|request\.variables)",
                          self._ref_text(ctx, p)) for p in refs.values() if p.endswith(".js")):
            warns.append("a script reads environment values")
        return changed, warns, foreign

    @staticmethod
    def _ref_text(ctx: dict, p: str) -> str:
        try:
            return common.safe_read(ctx["root"], p, MAX_FILE).decode("utf-8", "replace")
        except (OSError, ValueError):
            return ""

    def _allow_hosts(self, key: str, hosts: list[str]) -> None:
        with _STORE_LOCK:
            data = read_json(HOSTS_FILE, {})
            data[key] = sorted(set(data.get(key) or []) | set(hosts))
            os.makedirs(HTTP_DIR, mode=0o700, exist_ok=True)
            write_json(HOSTS_FILE, data)
        audit({"event": "http-hosts-allowed", "project": key, "hosts": hosts})

    # ------------------------------------------------------------ a response saved to a file
    def _save_target(self, ctx: dict, save_to, agent: bool = True) -> str:
        """The project file a response goes to: a bare name lands in development/; never a dot path,
        never a file git tracks, never a .http / env file. A path the isolation policy hides: never for
        a session; the GUI's run may (L3b, 2026-10-07 - the one place an unmasked response is safe), not
        while the policy cannot be read (everything looks hidden then)."""
        raw = str(save_to or "").strip()
        rel = self._norm(raw if "/" in raw.strip("/") else SAVE_DIR + "/" + raw)
        if any(p.startswith(".") for p in rel.split("/")):
            raise HttpError("save_to: %s - no hidden files or directories" % rel)
        if re.search(r"\.(http|rest)$", rel, re.I) or os.path.basename(rel) in (ENV_FILE, PRIVATE_FILE):
            raise HttpError("save_to: %s - not over a .http or an env file" % rel)
        if ctx["hidden"](rel) and (agent or getattr(ctx["hidden"], "unreadable", "")):
            raise HttpError("save_to: %s is hidden by the isolation policy" % rel if agent else
                            "save_to: the isolation policy cannot be read (%s) - Piper cannot tell whether %s is hidden "
                            "from Claude" % (ctx["hidden"].unreadable, rel))
        full = os.path.join(os.path.realpath(ctx["root"]), rel)
        if os.path.isdir(full):
            raise HttpError("save_to: %s is a directory - name the file" % rel)
        top = ctx["top"]
        if top:
            parent, base = os.path.split(full)
            try:   # a case-insensitive disk: README.md is overwritten by save_to readme.md - check the real name
                base = next((n for n in os.listdir(parent) if n.casefold() == base.casefold()), base)
            except OSError:
                pass
            try:
                tracked = gitops.is_tracked(top, os.path.relpath(os.path.join(parent, base), top))
            except gitops.GitError:
                tracked = True
            if tracked:
                raise HttpError("save_to: %s is tracked by git - pick an untracked name (development/ is the "
                                "project's scratch dir)" % rel)
        return rel

    def _save_response(self, ctx: dict, rel: str, row: dict, raw: bytes, secret_list: list[str],
                       agent: bool = True) -> dict:
        """Write a response into the project. The session reads the file unscrubbed, so a body holding a
        secret of the environment is written masked (text) or not at all (binary). The GUI's run into a
        file hidden from Claude (L3b): as received - an existing one rewritten IN PLACE (a rename would
        detach a live sandbox's over-mount, P0), a new one only while no sandbox of the tree runs
        (workbench._ops_guard), its temp file outside the project."""
        ctype = next((v for k, v in row["headers"] if k.lower() == "content-type"), "")
        data, note = raw, ""
        hidden = not agent and bool(ctx["hidden"](rel)) and not getattr(ctx["hidden"], "unreadable", "")
        root = ctx["root"]
        if hidden:
            note = "hidden from Claude - written as received"
        elif is_text(ctype, raw):
            text = raw.decode("utf-8", "replace")
            masked = redactor(secret_list)(text)
            if masked != text:
                data, note = masked.encode(), "secrets of the environment masked in the saved file"
        elif secret_hits(raw, secret_list):
            row["ok"] = False
            row["error"] = "the response holds a secret of the environment - not saved (a binary file cannot be masked)"
            return {}
        parts = rel.split("/")
        with self._write_lock:   # the check and the write as one step, like workbench.fs_write / fs_create
            old = None
            if hidden:
                try:
                    old = common.safe_read(root, rel, SAVE_HIDDEN_MAX + 1)
                    if len(old) > SAVE_HIDDEN_MAX:
                        raise ValueError("larger than %d MiB" % (SAVE_HIDDEN_MAX >> 20))
                except FileNotFoundError:
                    old = None
                except (OSError, ValueError) as e:
                    row["ok"] = False
                    row["error"] = "cannot write %s: %s" % (rel, e)
                    return {}
                try:
                    if old is None:   # a new hidden path: only where every live sandbox already hides it
                        self._ops_guard(self.gui_project(ctx["key"]), os.path.realpath(root), [(None, rel)])
                    else:             # an existing one: not where a live sandbox sees it (like fs_write)
                        self._hidden_write_guard(os.path.realpath(root), rel)
                except (ValueError, KeyError) as e:
                    row["ok"] = False
                    row["error"] = "not saved: %s" % e
                    return {}
            return self._save_write(row, root, rel, parts, data, hidden, old, note,
                                    self._ops_pin(os.path.realpath(root), rel) if hidden else None)

    @staticmethod
    def _save_write(row: dict, root: str, rel: str, parts: list, data: bytes, hidden: bool, old: bytes | None,
                    note: str, pin=None) -> dict:
        try:
            for i in range(1, len(parts)):   # missing directories, created without following links
                if not common.safe_is_dir(root, "/".join(parts[:i])):
                    fd = common._open_dir(root, parts[:i - 1])
                    try:
                        os.mkdir(parts[i - 1], 0o755, dir_fd=fd)
                    finally:
                        os.close(fd)
            if hidden:   # in place, with the redo journal (a rename would detach a live sandbox's over-mount)
                common.overwrite_in_place(root, rel, data, old or b"", create=old is None, pin=pin)   # N10
            else:
                common.safe_write(root, rel, data)
        except (OSError, ValueError) as e:
            row["ok"] = False
            row["error"] = "cannot write %s: %s" % (rel, e)
            return {}
        out = {"saved": rel, "saved_bytes": len(data)}
        if note:
            out["saved_note"] = note
        if hidden:
            out["saved_hidden"] = True   # _agent_view: a session's copy of the run does not name it
        return out

    # ------------------------------------------------------------ tunnels (the DB client's machinery)
    def _trusted_env_docs(self, ctx: dict, env_dir: str | None, public: dict, private: dict) -> list[dict]:
        """The env files a session can neither see nor write. None when the rule hides everything (the
        isolation settings cannot be read): fail-closed for reading is not "trusted" for a tunnel."""
        if ctx["hidden"]("piper-trust-probe-%s/x" % _secrets.token_hex(4)):
            return []
        pre = (env_dir + "/" if env_dir else "")
        import guard   # an allow-listed candidate (policy allow / library_allow) is mounted - the sandbox writes it
        allow = list((self.policy or {}).get("allow") or []) + guard.root_allow(ctx["root"], self.policy or {})
        return [d for n, d in ((ENV_FILE, public), (PRIVATE_FILE, private))
                if d and ctx["hidden"](pre + n) and not (allow and guard.hidden_by(allow, pre + n))]

    def _http_tunnel(self, ctx: dict, env: str, env_dir: str | None, public: dict, private: dict, cls: str) -> dict | None:
        """The tunnel an environment declares - honoured only from env files a session can neither see
        nor write (a planted declaration would make the host ssh anywhere with the user's keys)."""
        pre = (env_dir + "/" if env_dir else "")
        cfg = (self._http_envcfg(ctx["key"], env_dir, env).get("tunnel") or {"kind": "auto"})
        if cfg["kind"] == "none":
            return None
        if cfg["kind"] in ("ssh", "stack"):   # Piper's own settings (the GUI): host-only, always trusted
            vals = {}
            for d in (public, private):
                if isinstance(d.get(env), dict):
                    vals.update(flatten(d[env]))
            meta = {"ssh": cfg.get("ssh"), "forward": cfg.get("forward")} if cfg["kind"] == "ssh" \
                else {"tunnel": cfg.get("target")}
            if cfg.get("port"):
                meta["port"] = cfg["port"]
            spec = tunnel_spec(env, meta, vals, from_name=False)
            if spec and spec["kind"] == "stack" and spec["env"] != cls:
                raise HttpError("the tunnel of %r goes to %s, the environment's class is %s" % (env, spec["env"], cls))
            if spec:
                spec["source"] = "Piper settings"
            return spec
        safe = self._trusted_env_docs(ctx, env_dir, public, private)
        trusted = [d for d in safe if env in d]
        for d in (public, private):
            m = d.get(env, {}).get(META_KEY) if isinstance(d.get(env), dict) else None
            if isinstance(m, dict) and (m.get("ssh") or m.get("tunnel")) and not any(d is x for x in safe):
                raise HttpError("the tunnel of %r is declared in an env file sessions can see or write - Piper opens "
                                "tunnels only from env files hidden by the isolation policy (%s%s)" % (env, pre, ENV_FILE))
        if not trusted:
            return None
        meta, vals = {}, {}
        for d in trusted:
            e = d.get(env) if isinstance(d.get(env), dict) else {}
            if isinstance(e.get(META_KEY), dict):
                meta.update(e[META_KEY])
            vals.update(flatten(e))
        spec = tunnel_spec(env, meta, vals, from_name=True)
        if spec and spec["kind"] == "stack" and spec["env"] != cls:
            raise HttpError("the tunnel of %r goes to %s, the environment's class is %s" % (env, spec["env"], cls))
        if spec:
            spec["source"] = "env file" if meta.get("ssh") or meta.get("tunnel") else "environment name"
        return spec

    def _http_tunnel_conn(self, tun: dict, ctx: dict, cls: str) -> dict:
        """The DB client's pseudo-connection of an environment's tunnel (its key names the tunnel there)."""
        c = {"name": "http:%s:%s" % (ctx["key"], tun["label"]), "cls": cls, "engine": "http"}
        if tun["kind"] == "stack":
            c.update(tunnel={"env": tun["env"], "target": tun["target"]}, root=ctx["root"])
        else:
            c.update(ssh=tun["ssh"], host=tun["host"], port=tun["rport"])
        return c

    def _http_tunnel_open(self, tun: dict, ctx: dict, cls: str) -> int:
        """Piper's local port of the environment's tunnel - opened (or reused) like a DB connection's: a
        port Piper chose, closed when idle, a cluster sign-in shown in the Feed."""
        import dbtools
        c = self._http_tunnel_conn(tun, ctx, cls)
        try:
            if tun["kind"] == "stack":
                return self._db_tunnel_open(c, ctx.get("session"))
            return self._db_ssh_open(c, ctx.get("session"))
        except dbtools.DbError as e:
            raise HttpError("%s (Piper opens this tunnel for the environment; if you open it yourself, set the tunnel "
                            "to None in the HTTP tab's Environments settings)" % e) from None

    # ------------------------------------------------------------ a service in the session's own sandbox
    def _sandbox_python(self, s) -> list[str]:
        """argv prefix of python3 inside the session's agent container (tests replace it)."""
        from sessions import DOCKER_BIN
        return [DOCKER_BIN, "exec", "-i", s.instance + "-agent-1", "python3"]

    def _sandbox_route(self, ctx: dict, sel: list, values: dict) -> dict:
        """{"ports": {port: host}, "label"} when a session's run goes into its own sandbox: every request
        to localhost / 127.0.0.1:<port> and something listens on such a port INSIDE the session's sandbox
        (the agent's localhost - a service it started there). Else {} and the run goes to the host's
        localhost as before (a stack on the host, a tunnel)."""
        s = ctx.get("session")
        ports = local_ports([substitute(r["url"], values) for r in sel]) if s else None
        if not ports:
            return {}
        if not s.instance:
            s.instance = self._instance(s)
        if not s.instance or s.state not in ("ready", "working", "waiting", "attention"):
            return {}
        live = []
        for port, host in sorted(ports.items()):
            try:
                r = subprocess.run(self._sandbox_python(s) + ["-c", RELAY_SCRIPT, host, str(port), "probe"],
                                   capture_output=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if r.returncode == 0:
                live.append(port)
        if not live:
            return {}
        return {"ports": ports, "label": "this session's sandbox (%s)" % ", ".join(
            "%s:%d" % (h, p) for p, h in sorted(ports.items()))}

    def _sandbox_checks(self, ctx: dict, text: str, refs: dict, sel: list, values: dict) -> None:
        """On the sandbox route every host is fixed before the run: a script may not set a variable the
        scheme / host / port of a request URL uses. The path and query are free - the host is written out
        literally in front of them (pin_url), so a value set at run time cannot move the request."""
        names = url_var_names([url_head(r["url"])[0] for r in sel], values)
        if not names:
            return
        for src in [text] + [self._ref_text(ctx, p) for p in refs.values() if p.endswith(".js")]:
            for m in SCRIPT_SET_RX.finditer(src):
                arg = m.group(1).strip()
                lit = re.fullmatch(r"(['\"])([^'\"\\]*)\1", arg)
                if not lit:
                    raise HttpError("a script sets a variable by a computed name (%s) and a request's host uses a "
                                    "variable - a run into the session's sandbox needs names written out" % arg[:60])
                if lit.group(2) in names:
                    raise HttpError("a script sets {{%s}}, which the host of a request URL uses - a run into the "
                                    "session's sandbox fixes every host before it starts (a variable in the path "
                                    "or query is fine)" % lit.group(2))

    def _globals_key(self, key: str, rel: str, env: str) -> str:
        return "\0".join((key, rel, env))

    def _execute(self, ctx: dict, rel: str, env: str, index: int | None, agent: bool,
                 approve=None, include_body: bool = False, expect_sha: str = "", save_to: str = "",
                 text: str | None = None) -> dict:
        """Parse, check, (approve), snapshot and run; the stored record (full, for the GUI).
        save_to: request `index` is sent by piperd itself and its response written to that project
        file (ijhttp's console cannot carry a binary body)."""
        root, key = ctx["root"], ctx["key"]
        ctx = dict(ctx, rel=rel)
        binary = find_ijhttp(self._http_policy())
        if not binary and not save_to:
            raise HttpError("ijhttp is not installed on the host - Piper GUI: open a .http file and click "
                            "Install, or put `ijhttp` on piperd's PATH")
        if text is None:   # a session may send the file's text instead (written in its own scratch dir)
            text = self._http_read(root, rel)
        if len(text) > MAX_FILE:
            raise HttpError("%s is larger than 1 MiB" % rel)
        if expect_sha and hashlib.sha256(text.encode()).hexdigest() != expect_sha:
            raise HttpError("%s changed since you opened it - look at it again before running" % rel)
        if agent and ctx["hidden"](rel):
            raise HttpError("%s is hidden by the isolation policy" % rel)
        if agent:   # ijhttp ends a line at a lone CR too (verified, 2026.1): a request the checks never saw
            text = re.sub(r"\r(?!\n)", "\n", text)
        if agent and FORBIDDEN_VARS.search(text):
            raise HttpError("{{$dotenv}} / {{$processEnv}} are not available to a session")
        parsed = parse(text)
        if not parsed["requests"]:
            raise HttpError("%s holds no request" % rel)
        if index is not None and not 0 <= index < len(parsed["requests"]):
            raise HttpError("no request #%d in %s" % (index, rel))
        dirs = self._env_dirs(root)
        env_dir = self._env_dir_for(root, rel, dirs)
        public, private = self._load_env(root, env_dir)
        if env not in public and env not in private:
            raise HttpError("unknown environment %r - %s offers: %s" % (
                env, (env_dir or ".") + "/" + ENV_FILE,
                ", ".join(e["name"] for e in self._env_list(key, public, private)) or "none"))
        cls = self._env_class(key, env)
        pub_hidden = bool(public) and ctx["hidden"]((env_dir + "/" if env_dir else "") + ENV_FILE)
        gstore = read_json(GLOBALS_FILE, {})
        gl = dict(gstore.get(self._globals_key(key, rel, env)) or {}) if index is not None else {}
        values = dict(flatten(public.get(env) or {}), **flatten(private.get(env) or {}))
        values.update({v["name"]: substitute(v["value"], values) for v in parsed["vars"]})
        values.update({k: str(v) for k, v in gl.items()})
        refs = self._refs(ctx, rel, parsed, agent)
        sel = parsed["requests"] if index is None else [parsed["requests"][index]]
        save_rel = ""
        if save_to:
            if index is None:
                raise HttpError("save_to needs index: the response of ONE request goes to the file")
            if agent and cls == "prod" and not include_body:
                raise HttpError("saving a production response needs include_body (the user approves handing it over)")
            save_rel = self._save_target(ctx, save_to, agent)
        tdocs = self._trusted_env_docs(ctx, env_dir, public, private)
        _, sslconf = env_meta(*(tdocs + [{}, {}])[:2], env)   # a file a session writes must not turn TLS checks off
        vt = self._http_envcfg(key, env_dir, env).get("verify_tls")
        if vt is not None:   # Piper's own setting (the GUI) wins over the env file and the class policy
            sslconf = {"verifyHostCertificate": bool(vt), "forced": True}
        tun = self._http_tunnel(ctx, env, env_dir, public, private, cls)
        # a session's run to localhost where its own sandbox listens: into the sandbox through Piper's relay -
        # the agent's own server, so no environment class and no outside host (it could curl it itself)
        sbx = self._sandbox_route(ctx, sel, values) if agent and not tun else {}
        # hidden values of the environment would reach a server the agent runs: the environment's own class
        # (and its card) stays - a prod one is never "allowed for the session"
        sbx_secrets = bool(sbx) and any(env in d for d in tdocs)
        if sbx:
            self._sandbox_checks(ctx, text, refs, sel, values)
            if not sbx_secrets:
                cls = "dev"
        changed, warns, foreign = (False, [], [])
        if agent:
            changed, warns, foreign = self._agent_checks(ctx, text, sel, public, private, env, env_dir, refs, values)
            if sbx:
                foreign = [h for h in foreign if h not in LOCAL_HOSTS]
        secret_list = secret_values(public.get(env) or {}, private.get(env) or {}, pub_hidden)
        scrub = redactor(secret_list)
        brief = [{"name": r["name"], "method": r["method"], "url": scrub(substitute(r["url"], values))} for r in sel]
        # the snapshot - the text read above and its referenced files, copied BEFORE the card:
        # what runs is what was shown, whatever the sandbox writes meanwhile
        now = time.time()   # names sort by time: the newest run is the last name
        rid = time.strftime("%Y%m%d-%H%M%S-", time.localtime(now)) + "%06d" % int(now % 1 * 1e6) + _secrets.token_hex(2)
        work = os.path.join(HTTP_DIR, "tmp", rid)
        os.makedirs(HTTP_DIR, mode=0o700, exist_ok=True)
        os.makedirs(work, mode=0o700)
        try:
            mapping = {}
            digest = hashlib.sha256(text.encode())
            subst = {x for r in parsed["requests"] for x in r["subst"]}
            script_refs = {x for r in parsed["requests"] for x in r["scripts"]}
            scripts = {}   # referenced script files: shown on the card with the .http file
            for n, (ref, p) in enumerate(sorted(refs.items())):
                try:
                    data = common.safe_read(root, p, 50 << 20)
                except (OSError, ValueError) as e:
                    raise HttpError("cannot read %s: %s" % (ref, e)) from None
                dst = os.path.join(work, "ref%d-%s" % (n, os.path.basename(p)))
                with open(dst, "wb") as f:
                    f.write(data)
                mapping[ref] = dst
                digest.update(b"\0" + ref.encode() + b"\0" + data)
                if agent and ref in subst and FORBIDDEN_VARS.search(data.decode("utf-8", "replace")):
                    raise HttpError("%s: {{$dotenv}} / {{$processEnv}} are not available to a session" % ref)
                if ref in script_refs:
                    scripts[ref] = data.decode("utf-8", "replace")
            digest.update(b"\0save_to\0" + save_rel.encode())   # "allow this content" never covers a save it did not show
            # ...nor a tunnel / certificate setting changed since (Piper's settings in the GUI)
            digest.update(("\0tunnel\0%s\0tls\0%s\0sandbox\0%s" % (tun["label"] if tun else "", sorted(sslconf.items()),
                                                                      sbx.get("label", ""))).encode())
            if approve is not None:
                request_scope = sorted([[urllib.parse.urlsplit(r["url"]).hostname or "", r["method"].upper()] for r in brief])
                def collect_evidence():
                    # Counts and variable NAMES only. Values stay in this host-side snapshot.
                    evrows = []
                    budget = evidence.Budget()
                    seen = read_json(SEEN_HOSTS_FILE, None)
                    complete = seen is not None and seen.get("complete", False) or not os.path.isdir(RUNS_DIR)
                    known_hosts = set((seen or {}).get("hosts", []))
                    private_names = set((private.get(env) or {}).keys())
                    for r in sel:
                        budget.left()
                        actual_url = substitute(r["url"], values)
                        host = urllib.parse.urlsplit(actual_url).hostname or ""
                        method = r["method"].upper()
                        unknown = host in foreign
                        evrows.append({"text": scrub(host), "chips": ["UNKNOWN HOST" if unknown else cls.upper()]})
                        if complete and host not in known_hosts:
                            evrows.append({"text": "First request to this host"})
                        elif not complete and host not in known_hosts:
                            evrows.append({"text": "Could not check: earlier host history unavailable"})
                        body = r.get("body", "")
                        body_vars = body
                        ref = body_ref(body)
                        if ref and ref[0] in mapping:
                            with open(mapping[ref[0]], "rb") as f:
                                raw_body = f.read()
                            body_bytes = len(substitute(raw_body.decode("utf-8", "replace"), values).encode()) if ref[1] else len(raw_body)
                            body_vars = raw_body.decode("utf-8", "replace") if ref[1] else ""
                        else:
                            body_bytes = len(substitute(body, values).encode())
                        evrows.append({"text": "%s · %d B body · %d declared headers" % (method, body_bytes, len(r["headers"]))})
                        raw_refs = "\n".join([r["url"], body_vars] + [v for _, v in r["headers"]])
                        refs_used = set(re.findall(r"\{\{\s*([\w.-]+)\s*\}\}", raw_refs))
                        # Resolve references through file variables without copying their values to the card.
                        for _ in range(5):
                            refs_used |= {n for k in list(refs_used) for n in re.findall(r"\{\{\s*([\w.-]+)\s*\}\}", str(values.get(k, "")))}
                        names = sorted(n for n in refs_used if n in private_names or SECRET_KEY_RX.search(n))
                        if names:
                            evrows.append({"text": "Secret variables: " + ", ".join(names)})
                        if r.get("scripts") or r.get("pre") or scripts:
                            evrows.append({"text": "Could not check: scripts can change the request, body and headers"})
                    return evidence.box("What it sends", evrows)
                job = evidence.Job(collect_evidence, "What it sends")
                facts = job.initial()
                approve({"cls": cls, "env": env, "changed": changed, "warnings": warns, "requests": brief,
                         "save_to": save_rel, "tunnel": tun["label"] if tun else "", "sandbox": sbx.get("label", ""),
                         "sandbox_secrets": sbx_secrets,
                         "text": scrub(text), "include_body": include_body, "foreign": foreign,
                         "digest": digest.hexdigest(), "scripts": {k: scrub(v) for k, v in scripts.items()},
                         "evidence": facts, "_evidence_job": job, "request_scope": request_scope,
                         # a script (or a script file) can read the hidden values and return them
                         # transformed in a test name - the scrubbing cannot see that
                         "secret_scripts": bool(pub_hidden and (SCRIPT_RX.search(text) or scripts))})
            rows = [(ln, i) for i, ln in enumerate(text.split("\n"))] if index is None else single_rows(text, parsed, index)
            rows = cookie_jar_rows(rows)
            rows = instrument_rows(rows, global_names(text))
            pins = {}
            if sbx:
                rows = no_redirect_rows(rows, sel)
                rows, pins = pin_rows(rows, text, sel, values)
            linemap = [o for _, o in rows]
            run_text = rewrite_refs("\n".join(t for t, _ in rows), mapping)
            hf = os.path.join(work, os.path.basename(rel))
            with open(hf, "w", encoding="utf-8") as f:
                f.write(run_text)
            pol = self._http_policy()
            started = time.time()
            lock = self._http_lock(key)
            # a session's run waits at most 30 s for another run of the project (a card of up to
            # CARD_S came before it: card + wait + run stay inside piper-mcp's 900 s)
            if not lock.acquire(timeout=LOCK_WAIT_AGENT if agent else -1):
                raise HttpError("another .http run of this project is in progress - try again in a minute")
            raw, saved = b"", None
            relays, moved = [], {}   # the sandbox route: its relays, {relay port: the port the file names}
            try:
                t0 = time.time()
                env_path = os.path.join(root, env_dir) if env_dir is not None else None
                run_gl, run_values = gl, values
                ports = {}   # every localhost:<port> of the run -> Piper's port
                if tun:   # Piper's own tunnel: every localhost:<declared port> of the run points at Piper's port
                    ports[tun["port"]] = self._http_tunnel_open(tun, ctx, cls)
                for port, host in (sbx.get("ports") or {}).items():
                    relays.append(SandboxRelay(self._sandbox_python(ctx["session"]) + ["-c", RELAY_SCRIPT, host, str(port)]))
                    ports[port] = relays[-1].port
                    moved[relays[-1].port] = port
                def rw(v: str) -> str:
                    return ports_rewrite(v, ports)
                if ports:
                    env_path = os.path.join(work, "env")
                    os.makedirs(env_path, mode=0o700)
                    for n, d in ((ENV_FILE, public), (PRIVATE_FILE, private)):
                        if d:
                            write_json(os.path.join(env_path, n), rewrite_env(d, env, ports))
                    with open(hf, "w", encoding="utf-8") as f:
                        f.write(rw(run_text))
                    run_gl = {k: rw(v) if isinstance(v, str) else v for k, v in gl.items()}
                    run_values = {k: rw(v) for k, v in values.items()}
                # the CLI ignores IDEA's SSLConfiguration (2026.1, verified): Piper turns it into --insecure
                insecure = cls in (pol.get("insecure") or []) or bool(tun) or sslconf.get("verifyHostCertificate") is False
                if sslconf.get("forced"):
                    insecure = not sslconf["verifyHostCertificate"]
                limit = int(pol.get("timeout") or 180)
                if agent:   # the tunnel's wait counts against the session's deadline
                    limit = min(limit, max(30, AGENT_RUN_MAX - int(time.time() - t0)))
                if save_rel:
                    one = dict(sel[0], url=rw(pins[sel[0]["line"]])) if pins else sel[0]
                    row, raw = fetch_deadline(one, run_values, mapping, insecure, limit)
                    if pins and unrelay_headers(row):   # the relay's text/plain: the saved file is written by
                        row["body"] = ""                    # its real type, no body decoded from the bytes
                    if row["error"].startswith("unresolved "):
                        setters = global_setters(text, parsed)
                        names = [m.group(1) for m in VAR_USE_RX.finditer(row["error"])]
                        hint = ["{{%s}} is set by request #%d %r" % (n, setters[n]["index"], setters[n]["name"])
                                for n in names if n in setters]
                        if hint:
                            row["error"] += " - " + "; ".join(hint) + ": run it with index first (its globals are stored)"
                    console, xml = "", ""
                else:
                    rc, console, xml = run_ijhttp(binary, work, hf, env, env_path, run_gl, insecure, limit)
            finally:
                for x in relays:
                    x.close()
                lock.release()
            if save_rel and row["ok"]:
                saved = self._save_response(ctx, save_rel, row, raw, secret_list + cookie_values([row]), agent)
            if save_rel:
                rc = 0 if row["ok"] else 1
        finally:
            shutil.rmtree(work, ignore_errors=True)
        reqs = [row] if save_rel else merge(parse_console(console), parse_junit(xml))
        stopped = next((r for r in reqs if r.get("binary")), None) if not save_rel and not xml else None
        if stopped:   # the CLI stopped after a binary response: no report - tests and globals of this run are lost
            for r, q in zip(reqs, sel):
                if q["handler"] or q["handler_file"]:
                    r["tests"] = [{"name": "(not evaluated)", "ok": False, "message": "the ijhttp CLI stopped the run after "
                                   "a binary response and wrote no report: the tests and client.global values of this "
                                   "run are lost - run this request with index"}]
                    r["ok"] = False
            for r in sel[len(reqs):]:
                reqs.append({"name": r["name"], "method": r["method"], "url": substitute(r["url"], run_values),
                             "req_headers": [], "status": None, "status_text": "", "headers": [], "body": "",
                             "time_ms": None, "tests": [], "globals": {}, "ok": False,
                             "error": "not run - the ijhttp CLI stops a run after a binary response (request %r): run "
                                      "this request with index, and %r with index + save_to (run the requests that set "
                                      "its globals with index first)" % (stopped["name"], stopped["name"])})
            if all(r["ok"] for r in reqs):
                rc = 0   # a single binary request: answered fine, only the CLI's exit code says otherwise
        for r in reqs:   # the run's copy -> the file the user knows, with its line numbers
            if sbx and not save_rel:   # a binary body the relay passed as text/plain
                unrelay_binary(r)
            # ...and the ports it names, not the relay's
            r["url"], r["error"] = ports_rewrite(r["url"], moved), ports_rewrite(r["error"], moved)
            r["error"] = hint_error(relink(r["error"], hf, rel, linemap), r["url"], sbx.get("label", ""))
            for t in r["tests"]:
                t["message"] = relink(t["message"], hf, rel, linemap)
        console = relink(console, hf, rel, linemap)
        newg = dict(gl)
        for r in reqs:
            newg.update(r.pop("globals", {}) or {})
            r["body_size"] = len(raw) if save_rel else r.get("body_size", len(r["body"]))
            if saved:
                r.update(saved)
            if len(r["body"]) > MAX_BODY_GUI:
                r["body"] = r["body"][:MAX_BODY_GUI]
                r["body_cut"] = True
        if newg:
            with _STORE_LOCK:   # re-read: another run may have stored meanwhile
                gstore = read_json(GLOBALS_FILE, {})
                gstore[self._globals_key(key, rel, env)] = {k: v for k, v in newg.items()
                                                            if isinstance(v, (str, int, float, bool))}
                write_json(GLOBALS_FILE, gstore)
        tail = ""
        if not reqs:   # nothing parsed: the end of the console says why (a syntax error, Java missing) - raw for
            # the GUI (L3d); a session reads it from rec["agent"], scrubbed by _agent_view
            tail = ("\n".join(ln for ln in console.split("\n") if ln.strip() and not ln.startswith("\tat ")
                                   and "JAVA_TOOL_OPTIONS" not in ln)[-2000:])
        failed = sum(1 for r in reqs if not r["ok"])
        rec = {"id": rid, "project": key, "file": rel, "env": env, "cls": cls, "env_dir": env_dir,
               "index": index, "by": ctx["by"], "at": started, "ms": int((time.time() - started) * 1000),
               "rc": rc, "ok": rc == 0 and bool(reqs) and not failed, "requests": reqs,
               "summary": "%d request%s, %d failed" % (len(reqs), "" if len(reqs) == 1 else "s", failed),
               "output": tail, "changed": changed,
               "share": cls != "prod" or include_body}
        if tun:
            rec["tunnel"] = tun["label"]
        if sbx:
            rec["tunnel"] = rec["sandbox"] = sbx["label"]   # the GUI shows the route like a tunnel's
        self._http_store(rec, redactor(secret_list + cookie_values(reqs)))
        audit({"event": "http-run", "project": key, "file": rel, "env": env, "cls": cls, "by": ctx["by"],
               "index": index, "ok": rec["ok"], "requests": len(reqs), "failed": failed, "changed": changed,
               "saved": (saved or {}).get("saved"), "tunnel": tun["label"] if tun else None,
               "sandbox": sbx.get("label")})
        return rec

    def _http_store(self, rec: dict, scrub) -> None:
        with self._http_locks_lock:
            seen = read_json(SEEN_HOSTS_FILE, None)
            if seen is None:
                seen = {"hosts": [], "complete": not os.path.isdir(RUNS_DIR)}
            hosts = set(seen["hosts"])
            for req in rec.get("requests", []):
                host = urllib.parse.urlsplit(str(req.get("url") or "")).hostname
                if host:
                    hosts.add(host)
            write_json(SEEN_HOSTS_FILE, dict(seen, hosts=sorted(hosts)))
        os.makedirs(RUNS_DIR, mode=0o700, exist_ok=True)
        # the scrubbed copy for agents is computed now, while the secrets are known
        rec["agent"] = self._agent_view(rec, scrub)
        write_json(os.path.join(RUNS_DIR, rec["id"] + ".json"), rec)
        names = sorted(n for n in os.listdir(RUNS_DIR) if n.endswith(".json"))
        for n in names[:-KEEP_RUNS]:
            try:
                os.unlink(os.path.join(RUNS_DIR, n))
            except OSError:
                pass

    def _agent_view(self, rec: dict, scrub) -> dict:
        """What a session may read of a run: secrets and session headers masked, bodies cut."""
        limit = int(self._http_policy().get("body_limit") or 4000)
        out = []
        for r in rec["requests"]:
            item = {"name": r["name"], "method": r["method"], "url": scrub(r["url"]), "status": r["status"],
                    "time_ms": r["time_ms"], "ok": r["ok"], "error": scrub(r["error"]),
                    "tests": [{"name": t["name"], "ok": t["ok"], "message": scrub(t["message"])} for t in r["tests"]],
                    "headers": [[k, scrub(v)] for k, v in mask_headers(r["headers"])
                                if k.lower() in ("content-type", "location", "content-length", "content-disposition")],
                    "body_size": r.get("body_size", len(r["body"]))}
            for k in ("saved", "saved_bytes", "saved_note"):
                if r.get(k) and not r.get("saved_hidden"):
                    item[k] = r[k]
            ctype = next((v for k, v in r["headers"] if k.lower() == "content-type"), "")
            raw = r["body"]
            more = len(raw) - limit
            body = scrub(raw[:max(limit, 4096) + 512])   # the cut first: scrub never runs over a whole huge body
            if r.get("saved") and not r.get("saved_hidden"):   # a file hidden from Claude is never named to it
                body = body[:limit] + ("\n… (%d more characters - the whole response is in %s)" % (more, r["saved"])
                                       if more > 0 else "")
            elif r.get("binary") or body and ctype and not is_text(ctype) and ("\ufffd" in body[:4096] or "\0" in body[:4096]):
                clen = next((v for k, v in r["headers"] if k.lower() == "content-length"), "") or str(item["body_size"])
                body = "(binary %s, %s bytes - run this request with index + save_to to get it as a file)" % (
                    ctype.split(";")[0] or "response", clen)
            elif more > 0:
                body = body[:limit] + "\n… (%d more characters - index + save_to writes the whole response to a file)" % more
            item["body"] = body
            out.append(item)
        return {"requests": out, "output": scrub(rec.get("output") or "")}

    # ------------------------------------------------------------ records
    def http_runs(self, key: str, rel: str = "", limit: int = 30) -> list[dict]:
        if not os.path.isdir(RUNS_DIR):
            return []
        out = []
        for n in sorted(os.listdir(RUNS_DIR), reverse=True):
            if not n.endswith(".json"):
                continue
            r = read_json(os.path.join(RUNS_DIR, n), None)
            if not r or r.get("project") != key or rel and r.get("file") != rel:
                continue
            out.append({k: r.get(k) for k in ("id", "file", "env", "cls", "index", "by", "at", "ms", "ok",
                                              "summary", "share")})
            if len(out) >= limit:
                break
        return out

    def http_run_record(self, rid: str) -> dict:
        if not re.match(r"^[0-9]{8}-[0-9]{6}-[0-9]{6}[0-9a-f]{4}$", str(rid or "")):
            raise KeyError("unknown run %r" % rid)
        r = read_json(os.path.join(RUNS_DIR, rid + ".json"), None)
        if not r:
            raise KeyError("unknown run %r" % rid)
        r.pop("agent", None)
        return r

    def http_share(self, rid: str, on: bool) -> dict:
        self.http_run_record(rid)
        path = os.path.join(RUNS_DIR, rid + ".json")
        r = read_json(path, {})
        r["share"] = bool(on)
        write_json(path, r)
        return {"id": rid, "share": r["share"]}

    # ------------------------------------------------------------ GUI
    def http_gui_envs(self, key: str, rel: str) -> dict:
        return self.http_envs(self._http_gui_ctx(key), rel)

    def http_gui_files(self, key: str, limit: int = 400) -> dict:
        """The HTTP tab: every .http/.rest file of the project with its requests and its last run - one
        hidden from Claude too, flagged `hidden` (the GUI's, 2026-10-07; a session's http_last_result
        never returns a run of a file hidden from it)."""
        ctx = self._http_gui_ctx(key)
        real = os.path.realpath(ctx["root"])
        found, truncated = [], False
        for d, dirs, files in os.walk(real, followlinks=False):
            drel = os.path.relpath(d, real)
            drel = "" if drel == "." else drel.replace(os.sep, "/")
            dirs[:] = sorted(x for x in dirs if x.lower() != ".git" and x not in projectfs.SKIP and x not in SKIP_DIRS)
            for fn in sorted(files):
                rel = (drel + "/" if drel else "") + fn
                if re.search(r"\.(http|rest)$", fn, re.I) and not os.path.islink(os.path.join(d, fn)):
                    found.append(rel)
            if len(found) >= limit:
                found, truncated = found[:limit], True
                break
        last = {}
        for r in self.http_runs(ctx["key"], "", limit=KEEP_RUNS):   # newest first: the first per file wins
            last.setdefault(r["file"], r)
        out = []
        for rel in found:
            item = {"file": rel, "requests": [], "last": last.get(rel), "hidden": bool(ctx["hidden"](rel))}
            try:
                item["requests"] = [{k: q[k] for k in ("index", "name", "method", "url")}
                                    for q in parse(self._http_read(ctx["root"], rel)[:MAX_FILE])["requests"]]
            except HttpError as e:
                item["error"] = str(e)
            out.append(item)
        return {"files": out, "truncated": truncated, **self.http_status()}

    def http_gui_run(self, key: str, rel: str, env: str, index: int | None, sha: str = "", save_to: str = "") -> dict:
        """sha: of the text the user saw - a file a session rewrote meanwhile does not run (409).
        save_to: request `index`'s whole response into that project file (a binary one: a PDF)."""
        rec = self._execute(self._http_gui_ctx(key), self._norm(rel), str(env or ""), index, agent=False,
                            expect_sha=str(sha or ""), save_to=str(save_to or ""))
        rec.pop("agent", None)
        return rec

    # ------------------------------------------------------------ environment settings (GUI)
    def _http_envcfg(self, key: str, env_dir: str | None, env: str) -> dict:
        return dict(((read_json(ENVCFG_FILE, {}) or {}).get(key) or {}).get("%s\0%s" % (env_dir or "", env)) or {})

    def _http_env_ctx(self, key: str, rel: str) -> tuple[dict, str | None, dict, dict]:
        """(GUI ctx, env dir, public, private) of a .http file - or of an env directory given as `dir/`."""
        ctx = self._http_gui_ctx(key)
        rel = str(rel or "")
        if rel.endswith("/") or rel in ("", "."):
            env_dir = rel.strip("/")
            if env_dir:
                env_dir = self._norm(env_dir)
            if env_dir not in self._env_dirs(ctx["root"]):
                raise HttpError("no %s / %s in %s" % (ENV_FILE, PRIVATE_FILE, env_dir or "the project root"))
        else:
            rel = self._norm(rel)
            env_dir = self._env_dir_for(ctx["root"], rel, self._env_dirs(ctx["root"]))
        public, private = self._load_env(ctx["root"], env_dir)
        return ctx, env_dir, public, private

    def _http_tunnel_view(self, ctx: dict, env: str, env_dir, public: dict, private: dict, cls: str) -> dict:
        try:
            t = self._http_tunnel(ctx, env, env_dir, public, private, cls)
        except HttpError as e:
            return {"error": str(e)}
        if not t:
            return {}
        state = self._db_tunnel_state(self._http_tunnel_conn(t, ctx, cls))
        import dbtools
        via, to = (dbtools.ssh_label(t), "%s:%s" % (t["host"], t["rport"])) if t["kind"] == "ssh" \
            else ("dev stack", "%s/%s" % (t["env"], t["target"]))
        return {"label": t["label"], "source": t.get("source", ""), "kind": t["kind"], "local": t["port"],
                "via": via, "to": to,   # the route the GUI draws: localhost:<local> -> via -> to
                "state": state.get("state") or "closed", "port": state.get("port"), "url": state.get("url", "")}

    @staticmethod
    def _http_env_path(rel: str) -> str:
        """Only the two HTTP env basenames, with no traversal or symlink normalization."""
        if not isinstance(rel, str) or "\x00" in rel or rel.startswith("/") or guard.is_git_path(rel) or "\\" in rel or any(
                part in ("", ".", "..", ".git") for part in rel.split("/")):
            raise HttpError("an HTTP env file inside the project is required")
        if rel.split("/")[-1] not in (ENV_FILE, PRIVATE_FILE):
            raise HttpError("file must name an HTTP env JSON file")
        return rel

    @staticmethod
    def _http_env_document(text: str) -> dict:
        def unique(pairs):
            out = {}
            for key, value in pairs:
                if key in out:
                    raise ValueError("duplicate JSON key")
                out[key] = value
            return out
        try:
            doc = json.loads(text, object_pairs_hook=unique)
            json.dumps(doc, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise HttpError("invalid environment JSON") from None
        if not isinstance(doc, dict) or any(not isinstance(v, dict) for v in doc.values()):
            raise HttpError("environment JSON must contain objects keyed by environment name")
        return doc

    def http_gui_envfile(self, key: str, rel: str) -> dict:
        """GUI-only full document; no equivalent session read tool."""
        ctx = self._http_gui_ctx(key)
        rel = self._http_env_path(rel)
        try:
            raw = common.safe_read(ctx["root"], rel, MAX_FILE + 1, single_link=True)
        except FileNotFoundError:
            return {"text": "{}", "sha": "", "exists": False}
        if len(raw) > MAX_FILE:
            raise HttpError("environment file is too large")
        return {"text": raw.decode("utf-8"), "sha": hashlib.sha256(raw).hexdigest(), "exists": True}

    def _http_env_write(self, ctx: dict, rel: str, text: str, sha: str) -> dict:
        """Write under the existing file lock; preserve hidden file mounts and refuse stale edits."""
        self._http_env_document(text)
        data = text.encode("utf-8")
        if len(data) > MAX_FILE:
            raise HttpError("environment file is too large")
        root = ctx["root"]
        with self._write_lock:
            try:
                cur = common.safe_read(root, rel, MAX_FILE + 1, single_link=True)
                mode = common.safe_stat(root, rel).st_mode & 0o7777
                exists = True
            except FileNotFoundError:
                cur, mode, exists = b"", 0o600, False
            if (hashlib.sha256(cur).hexdigest() if exists else "") != sha:
                raise HttpError("environment file changed; reopen it and retry")
            if ctx["hidden"](rel):
                self._hidden_write_guard(root, rel)
                common.overwrite_in_place(root, rel, data, cur, mode=mode, exact_mode=True,
                                          create=not exists, pin=self._ops_pin(root, rel))
            else:
                common.safe_write(root, rel, data, mode=mode, exact_mode=True)
        audit({"event": "http-env-write", "project": ctx["key"], "path": rel, "by": ctx["by"]})
        self.bump()
        return {"ok": True}

    def http_gui_envfile_save(self, key: str, rel: str, text: str, sha: str) -> dict:
        return self._http_env_write(self._http_gui_ctx(key), self._http_env_path(rel), text, sha)

    def _http_env_set(self, ctx: dict, s, args: dict) -> dict:
        """Blind upsert. A card contains only proposed values; old content never leaves this method."""
        rel = self._http_env_path(args.get("file"))
        env, values = args.get("env"), args.get("values")
        if not isinstance(env, str) or not env.strip() or not isinstance(values, dict) or not values:
            raise HttpError("env and a nonempty values object are required")
        if any(not isinstance(k, str) or not k for k in values):
            raise HttpError("variable names must be nonempty strings")
        proposal = json.dumps(values, ensure_ascii=False, allow_nan=False)
        if len(proposal.encode("utf-8")) > 16000:
            raise HttpError("proposed values are too large for an approval card")
        try:
            raw = common.safe_read(ctx["root"], rel, MAX_FILE + 1, single_link=True)
            sha = hashlib.sha256(raw).hexdigest()
            if len(raw) > MAX_FILE:
                raise HttpError("environment file is too large")
            doc = self._http_env_document(raw.decode("utf-8"))
        except FileNotFoundError:
            doc, sha = {}, ""
        except (OSError, UnicodeError):
            raise HttpError("cannot access environment file") from None
        doc.setdefault(env, {}).update(json.loads(proposal))
        updated = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
        if len(updated.encode("utf-8")) > MAX_FILE:
            raise HttpError("environment file is too large")
        summary = "Update HTTP environment %s in %s\nNew values only (existing values stay private):\n%s" % (
            env, rel, proposal)
        if not self.ask("http_env_set", s, summary, {"file": rel, "env": env}, timeout=CARD_S):
            raise HttpError("environment write was not approved; nothing changed")
        try:
            return self._http_env_write(ctx, rel, updated, sha)
        except (OSError, ValueError) as e:
            if isinstance(e, HttpError):
                raise
            raise HttpError("cannot write environment file safely; check file isolation in Piper") from None

    def http_gui_envcfg(self, key: str, rel: str, targets: bool = False) -> dict:
        """The Environments dialog: per environment its class, Piper's settings, the tunnel in effect (and
        where it comes from) with its state; the env files (hidden or not); the dev stack's tunnel targets."""
        ctx, env_dir, public, private = self._http_env_ctx(key, rel)
        pre = (env_dir + "/" if env_dir else "")
        envs = []
        for e in self._env_list(key, public, private):
            _, ssl_env = env_meta(public, private, e["name"])
            envs.append(dict(e, cfg=self._http_envcfg(key, env_dir, e["name"]),
                             tunnel=self._http_tunnel_view(ctx, e["name"], env_dir, public, private, e["cls"]),
                             env_file_tls=ssl_env.get("verifyHostCertificate"),
                             insecure_by_class=e["cls"] in (self._http_policy().get("insecure") or [])))
        files = {}
        for which, n in (("public", ENV_FILE), ("private", PRIVATE_FILE)):
            files[which] = {"path": pre + n, "exists": os.path.isfile(os.path.join(ctx["root"], pre + n)),
                            "hidden": bool(ctx["hidden"](pre + n))}
        # the stack's targets only for the dialog: asking the stack can take long (a cold scan), the HTTP bar must not wait
        tg = [t for t in (self._db_tunnel_targets(ctx["root"]) if targets and hasattr(self, "_db_tunnel_targets") else [])
              if t.get("env") in ("test", "prod")]
        env_files = []
        for directory in self._env_dirs(ctx["root"]):
            for name in (ENV_FILE, PRIVATE_FILE):
                path = (directory + "/" if directory else "") + name
                env_files.append({"path": path, "exists": os.path.isfile(os.path.join(ctx["root"], path)),
                                  "hidden": bool(ctx["hidden"](path))})
        return {"env_dir": env_dir, "envs": envs, "files": files, "env_files": env_files,
                "targets": tg, "targets_loaded": bool(targets)}

    def http_gui_envcfg_save(self, key: str, rel: str, env: str, cfg: dict) -> dict:
        """Store (or clear) Piper's settings of one environment: tunnel and certificate check."""
        ctx, env_dir, public, private = self._http_env_ctx(key, rel)
        if env not in public and env not in private:
            raise HttpError("unknown environment %r" % env)
        if not isinstance(cfg, dict):
            raise HttpError("cfg must be an object")
        out = {}
        t = cfg.get("tunnel") or {"kind": "auto"}
        if not isinstance(t, dict) or t.get("kind") not in TUNNEL_KINDS:
            raise HttpError("tunnel.kind: one of %s" % ", ".join(TUNNEL_KINDS))
        if t["kind"] != "auto":
            tt = {"kind": t["kind"]}
            if t.get("port") not in (None, ""):
                p = t["port"]
                if isinstance(p, str) and p.isdigit():
                    p = int(p)
                if isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= 65535:
                    raise HttpError("the local port must be a number 1-65535")
                tt["port"] = p
            if t["kind"] == "ssh":
                tt.update(ssh=str(t.get("ssh") or "").strip(), forward=str(t.get("forward") or "").strip())
            elif t["kind"] == "stack":
                tt["target"] = str(t.get("target") or "").strip()
            if t["kind"] in ("ssh", "stack"):   # validated the way a run will read it
                vals = {}
                for d in (public, private):
                    if isinstance(d.get(env), dict):
                        vals.update(flatten(d[env]))
                meta = {"ssh": tt.get("ssh"), "forward": tt.get("forward")} if t["kind"] == "ssh" else {"tunnel": tt.get("target")}
                if tt.get("port"):
                    meta["port"] = tt["port"]
                spec = tunnel_spec(env, meta, vals, from_name=False)
                if not spec:   # an empty form must not silently switch the automatic tunnel off
                    raise HttpError("an SSH tunnel needs the server and the forward" if t["kind"] == "ssh"
                                    else "a dev stack tunnel needs a target")
                cls = self._env_class(key, env)
                if spec and spec["kind"] == "stack" and spec["env"] != cls:
                    raise HttpError("the tunnel goes to %s, the environment's class is %s" % (spec["env"], cls))
            out["tunnel"] = tt
        vt = cfg.get("verify_tls")
        if vt is not None:
            if not isinstance(vt, bool):
                raise HttpError("verify_tls: true, false or null")
            out["verify_tls"] = vt
        k = "%s\0%s" % (env_dir or "", env)
        with _STORE_LOCK:
            data = read_json(ENVCFG_FILE, {}) or {}
            proj = data.setdefault(key, {})
            if out:
                proj[k] = out
            else:
                proj.pop(k, None)
            os.makedirs(HTTP_DIR, mode=0o700, exist_ok=True)
            write_json(ENVCFG_FILE, data)
        audit({"event": "http-envcfg", "project": key, "env_dir": env_dir, "env": env, "cfg": out})
        self.bump()
        return {"ok": True, "cfg": out}

    def http_gui_tunnel(self, key: str, rel: str, env: str, op: str) -> dict:
        """Open / close / test an environment's tunnel from the GUI (test = open + a TCP connect)."""
        ctx, env_dir, public, private = self._http_env_ctx(key, rel)
        cls = self._env_class(key, env)
        t = self._http_tunnel(ctx, env, env_dir, public, private, cls)
        if not t:
            raise HttpError("%r has no tunnel" % env)
        if op == "close":
            c = self._http_tunnel_conn(t, ctx, cls)
            self._db_tunnel_close(self._db_tkey(c), "closed in the HTTP tab")
            audit({"event": "http-tunnel", "project": key, "env": env, "op": op, "tunnel": t["label"]})
            return self._http_tunnel_view(ctx, env, env_dir, public, private, cls)
        if op not in ("open", "test"):
            raise HttpError("op: open, close or test")
        started = time.time()
        port = self._http_tunnel_open(t, ctx, cls)
        out = self._http_tunnel_view(ctx, env, env_dir, public, private, cls)
        if op == "test":   # the local end always accepts: only an answer from BEHIND the tunnel proves the forward
            got = tunnel_probe(port)
            ms = int((time.time() - started) * 1000)
            out["test"] = {"answers": "the server behind the tunnel answers (%d ms)" % ms,
                           "closed": "the tunnel is up, but nothing answers behind it - check the forward / target",
                           "silent": "connected, no reply within 5 s (%d ms)" % ms,
                           "down": "no answer on 127.0.0.1:%d - the tunnel is not listening" % port}[got]
            out["test_ok"] = got == "answers"
        audit({"event": "http-tunnel", "project": key, "env": env, "op": op, "tunnel": t["label"]})
        return out

    def http_install_start(self) -> dict:
        if self._http_install["state"] == "running":
            return self.http_status()
        self._http_install.update(state="running", error="")
        threading.Thread(target=self._http_install_run, daemon=True, name="piper-ijhttp").start()
        self.bump()
        return self.http_status()

    def _http_install_run(self) -> None:
        dest = os.path.join(common.PIPER_HOME, "ijhttp")
        tmp = os.path.join(common.PIPER_HOME, ".ijhttp-new")
        z = os.path.join(common.PIPER_HOME, ".ijhttp.zip")
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            with urllib.request.urlopen(IJHTTP_URL, timeout=60) as r, open(z, "wb") as f:   # noqa: S310 - fixed https URL
                shutil.copyfileobj(r, f)
            with zipfile.ZipFile(z) as zf:
                for info in zf.infolist():
                    target = os.path.realpath(os.path.join(tmp, info.filename))
                    if not target.startswith(os.path.realpath(tmp) + os.sep):
                        raise HttpError("unsafe path in the ijhttp archive: %s" % info.filename)
                zf.extractall(tmp)
            inner = os.path.join(tmp, "ijhttp")
            launcher = os.path.join(inner, "ijhttp")
            if not os.path.isfile(launcher):
                raise HttpError("the archive has no ijhttp/ijhttp launcher")
            os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IXUSR)
            shutil.rmtree(dest, ignore_errors=True)
            os.replace(inner, dest)
            self._http_install.update(state="done", error="")
            audit({"event": "ijhttp-install", "ok": True, "path": dest})
        except Exception as e:  # noqa: BLE001 - the GUI shows why
            self._http_install.update(state="failed", error=str(e)[:500])
            audit({"event": "ijhttp-install", "ok": False, "error": str(e)[:500]})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                os.unlink(z)
            except OSError:
                pass
            self.bump()

    # ------------------------------------------------------------ session tools
    def http_tool(self, s, tool: str, args: dict):
        ctx = self._http_session_ctx(s)
        if tool == "http_env_set":
            return self._http_env_set(ctx, s, args)
        if tool == "http_envs":
            rel = args.get("file")
            if rel:
                if ctx["hidden"](self._norm(rel)):
                    raise HttpError("%s is hidden by the isolation policy" % rel)
                r = self.http_envs(ctx, str(rel))
                for k in ("install_error", "urls"):   # names only for a session, never the values
                    r.pop(k, None)
                r["tunnels"] = {e: (v if v.startswith("error:") else "Piper opens a tunnel for this environment")
                                for e, v in (r.get("tunnels") or {}).items()}
                return r
            dirs = self._env_dirs(ctx["root"])
            out = []
            for d in dirs:
                pub, priv = self._load_env(ctx["root"], d)
                out.append({"env_dir": d, "envs": self._env_list(ctx["key"], pub, priv)})
            return {"env_dirs": out, "ijhttp": bool(find_ijhttp(self._http_policy()))}
        if tool == "http_last_result":
            rel = self._norm(args["file"]) if args.get("file") else ""
            if rel and ctx["hidden"](rel):
                raise HttpError("%s is hidden by the isolation policy" % rel)
            for r in self.http_runs(ctx["key"], rel, limit=50):
                if not ctx["hidden"](r["file"]):
                    return self._agent_record(r["id"])
            raise HttpError("no run of %s yet" % (rel or "any .http file of this project"))
        if tool != "http_run":
            raise ValueError("unknown tool %r" % tool)
        rel, env = self._norm(args.get("file")), str(args.get("env") or "")
        index = args.get("index")
        if index is not None and (not isinstance(index, int) or isinstance(index, bool)):
            raise HttpError("index must be an integer (0 = the first request)")
        include = args.get("include_body") is True
        save_to = args.get("save_to")
        if save_to is not None and (not isinstance(save_to, str) or not save_to.strip()):
            raise HttpError("save_to must be a file name or a project-relative path")
        reason = " ".join(str(args.get("reason") or "").split())[:500]

        def approve(info: dict) -> None:
            """At most ONE card per run (two in a row could outlast the MCP's 900 s - the sandbox would
            time out, the run still happen, and a retry run it twice)."""
            cls, foreign = info["cls"], info["foreign"]
            # "Allow this content for the session" holds for exactly this file + references on this env
            ok_key = (s.id, env, info["digest"])
            # a tunnel starts ssh / a cluster port-forward on the host: never without the user, also on "dev"
            needs = cls != "dev" or ("http_dev" in self.policy.get("ask", [])) or self._http_policy().get("card_dev") or \
                info["secret_scripts"] or info["tunnel"] or info["sandbox_secrets"]
            if not foreign and (not needs or cls != "prod" and ok_key in self._http_session_ok):
                return
            hosts = ", ".join(foreign)
            writes = [r["method"] for r in info["requests"] if r["method"].upper() not in ("GET", "HEAD", "OPTIONS")]
            # what matters first (the queue shows the first lines): the requests, then the details
            lines = ["HTTP requests on %s - environment %r, project %s" % (cls.upper(), env, ctx["key"])]
            if writes:
                lines.append("⚠ %d of them change data (%s)" % (len(writes), ", ".join(sorted(set(writes)))))
            lines += ["%d. %s %s" % (n + 1, r["method"], r["url"]) for n, r in enumerate(info["requests"])]
            if info["save_to"]:
                lines.append("The response is saved to %s - the agent reads that file (a secret of the environment "
                             "is masked in text; a binary file holding one is not saved)" % info["save_to"])
            if info["tunnel"]:
                lines.append("Through Piper's tunnel: %s" % info["tunnel"])
            if info["sandbox"]:
                lines.append("Into %s - the agent runs that server: it sees everything sent there%s" % (
                    info["sandbox"], ", the HIDDEN values of the environment too" if info["sandbox_secrets"] else ""))
            lines += ["", "File: %s%s" % (rel, "" if index is None else " (request #%d only)" % index)]
            if foreign:   # a host outside the environment: never auto-approved (http_host is in ALWAYS_ASK)
                lines += ["Host(s) NOT in the environment: %s - a script runs on the host with the environment's "
                          "secrets in reach; allow only hosts you know." % hosts]
            if reason:
                lines.append("Why: " + reason)
            if cls == "prod":
                lines.append("Response bodies to the agent: %s" % ("YES" if include else "no (status and tests only)"))
            lines += ["Warning: " + w for w in info["warnings"]]
            if info["secret_scripts"]:
                lines.append("Warning: the file has scripts - they can read the hidden environment values and return "
                             "them to the agent in a form the masking cannot recognise (a test name). Read them.")
            # all of it, always: "same as HEAD" proves nothing - the sandbox can commit
            lines += ["", "The file%s:" % (" (differs from git HEAD)" if info["changed"] else ""), "", info["text"][:20000]]
            for ref, body in info["scripts"].items():
                lines += ["", "Script %s:" % ref, "", body[:20000]]
            res: dict = {}
            kind = "http_host" if foreign else "http_" + cls
            # An outside host must not bypass a selected environment approval (still one card).
            if foreign and isinstance(self.policy.get("ask"), list) and "http_host" not in self.policy["ask"]:
                kind = "http_" + cls
            always = "Allow these hosts for this project" if foreign else \
                "Allow this content for the session" if cls != "prod" else ""
            ok = self.ask(kind, s, "\n".join(lines), {"file": rel, "env": env, "cls": cls, "hosts": foreign,
                          "project": ctx["key"], "request_scope": info["request_scope"], "evidence": info["evidence"],
                          "request_line": "\n".join(r["method"] + " " + r["url"] for r in info["requests"]),
                          "content_sha": info["digest"], "_evidence_job": info["_evidence_job"]},
                          timeout=CARD_S, always=always, result=res)
            if not ok and not res.get("answered", True):
                raise HttpError("nobody answered the approval card within %d minutes - nothing ran; ask the user "
                                "to watch the Piper GUI and call http_run again" % (CARD_S // 60))
            if not ok:
                raise HttpError(("the user declined host(s) %s - a session's run may only call hosts of the "
                                 "environment or hosts the user allowed" % hosts) if foreign
                                else "the user declined the run on %s" % cls)
            if res.get("always"):
                if foreign:
                    self._allow_hosts(ctx["key"], foreign)
                elif cls != "prod":
                    self._http_session_ok.add(ok_key)

        inline = args.get("text")
        if inline is not None and (not isinstance(inline, str) or not inline.strip()):
            raise HttpError("text must be the .http content")
        rec = self._execute(ctx, rel, env, index, agent=True, approve=approve, include_body=include,
                            save_to=save_to or "", text=inline)
        return self._agent_record(rec["id"])

    def _agent_record(self, rid: str) -> dict:
        r = read_json(os.path.join(RUNS_DIR, rid + ".json"), None)
        if not r:
            raise HttpError("unknown run %s" % rid)
        view = r.get("agent") or {"requests": []}
        reqs = view["requests"]
        if not r.get("share"):
            reqs = [{k: v for k, v in x.items() if k != "body"} for x in reqs]
        out = {"run": r["id"], "file": r["file"], "env": r["env"], "class": r["cls"], "by": "user (GUI)"
               if r["by"] == "gui" else "session", "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["at"])),
               "ok": r["ok"], "summary": r["summary"], "requests": reqs}
        if r.get("index") is not None:
            out["request_index"] = r["index"]
        if r.get("sandbox"):
            out["sandbox"] = "into this session's sandbox through Piper's relay"
        elif r.get("tunnel"):
            out["tunnel"] = "through Piper's tunnel"
        if not r.get("share"):
            out["bodies"] = "withheld (production) - the user can share them from the Piper GUI"
        if view.get("output"):
            out["output"] = view["output"]
        return out
