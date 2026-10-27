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
  - native `client.log` output appears nowhere without the private print metadata channel;
    a request has ONE response handler (a second
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
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

import common
import quietwindow
import evidence
import gitops
import guard
import httpauth
import httpdeps
import httpgraph
import httpcompat
import httpbroker
import httpcerts
import httpcookies
import httpjail
import httpruntime
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
    "connect_timeout_ms": 5000,
    "socket_timeout_ms": 60000,
    "body_limit": 4000,       # characters of a response body an agent gets
    "insecure": ["dev"],      # classes run with --insecure (self-signed dev certificates)
    "card_dev": False,        # a card for a session's dev run too
    "classes": {},            # {project key: {env name: dev|test|prod}} - overrides the name rule
}
IJHTTP_URL = "https://jb.gg/ijhttp/latest"
SKIP_DIRS = {"node_modules", ".git", ".idea", ".gradle", ".next", ".pnpm-store", "target", "build", "dist"}
VAR_RX = re.compile(r"^@([A-Za-z_][\w.-]*)\s*=\s*(.*?)\s*$")
NAME_RX = re.compile(r"^(?:#|//)\s*@name\s*=?\s*(.+?)\s*$")
REQ_RX = re.compile(r"^(?:([!#$%&'*+.^_`|~0-9A-Za-z-]+)\s+)?(\S.*?)(?:\s+HTTP/[\d.]+)?\s*$")
REF_RX = re.compile(r"^<@?\s+(?!\{%)(\S.*?)\s*$")          # `< body.json`, `<@ body.json` (with {{vars}})
SUBST_REF_RX = re.compile(r"^<@\s+")
SAVE_RX = re.compile(r"^>>!?\s+(\S.*?)\s*$")                   # `>> out.json`: ijhttp writes the response there
IMPORT_RX = re.compile(r'^import\s+(\S.*?)\s*$')
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
LOG_CAPTURE = CAPTURE + 'log:v1:'
MAX_SCRIPT_LOG = 64 << 10   # characters per response handler, bounded before JUnit serialization
# The unmodified pinned CLI cancels its pending binary download after this marker.
# httpcompat fixes the tested revision; unknown revisions retain the honest failure path.
BINARY_MARK = "<Binary response body is ignored"


_STORE_LOCK = threading.Lock()   # read-modify-write of globals.json / hosts.json


class HttpError(ValueError):
    """A refused or failed run (piperd answers 409 / the tool answers an error)."""


# ------------------------------------------------------------------ the .http file
def common_blocks(text: str) -> list[dict]:
    """Common script declarations belong to the file preamble, never an HTTP body."""
    lines, blocks, i = text.split('\n'), [], 0
    while i < len(lines):
        line = lines[i].strip()
        match = re.match(r'^=\s*\{%', line)
        if match:
            start = i
            while i < len(lines) and '%}' not in lines[i][(lines[i].index('{%') + 2 if i == start else 0):]:
                i += 1
            if i == len(lines):
                raise HttpError('unterminated HTTP common script block')
            code = '\n'.join(lines[start:i + 1])
            blocks.append(dict(start=start, end=i, code=code[code.index('{%') + 2:code.rfind('%}')]))
        elif line and not line.startswith(('#', '//', '@')):
            break
        elif line.startswith('###'):
            break
        i += 1
    return blocks


def common_rows(rows: list, requests: list[dict], blocks: list[list[dict]], mapping: dict,
                work: str, owners: list[str], script_maps: dict | None = None, module_parsed: dict | None = None) -> list:
    """Adapt common scope in private authored scripts without executing added handlers."""
    out = list(rows)
    for block in common_blocks('\n'.join(line for line, _ in rows)):
        for line in range(block['start'], block['end'] + 1):
            out[line] = ('', rows[line][1])
    expanded = {}
    for request, declarations, owner in zip(requests, blocks, owners):
        if not declarations:
            continue
        common_lines = [(line, (owner, block['start'] + offset)) for block in declarations
                        for offset, line in enumerate(httpdeps.relocate_modules(block['code'], owner,
                            os.path.join(work, 'project'), module_parsed).split('\n'))]
        code = '\n'.join(line for line, _ in common_lines) + '\n'
        for span in (request['pre'], request['handler']):
            if span:
                line, origin = out[span[0]]
                before, after = line.split('{%', 1)
                expanded[span[0]] = [(before + '{% ' + common_lines[0][0], common_lines[0][1]),
                                    *common_lines[1:], (after, origin)]
        for ref in request['scripts']:
            source = mapping[ref]
            target = os.path.splitext(source)[0] + '-piper-common-%d.js' % request['index']
            with open(source, encoding='utf-8') as original:
                body = original.read()
            with open(target, 'w', encoding='utf-8') as private:
                private.write(code + body)
            if script_maps is not None:
                script_maps[target] = [origin for _, origin in common_lines] + script_maps[source]
            mapping[target] = target
            for i in range(request['start'], request['end']):
                match = REF_RX.match(out[i][0].strip()) or HANDLER_FILE_RX.match(out[i][0].strip())
                if match and match[1] == ref:
                    out[i] = (out[i][0].replace(ref, target, 1), out[i][1])
    return [row for index, row in enumerate(out) for row in expanded.get(index, [row])]


def parse(text: str) -> dict:
    """Requests of an IntelliJ .http file: name, method, url, the request line, the line span of
    the block, headers, referenced files, response handler span. `vars`: the file variables
    (@name = value) in file order, with their line numbers."""
    lines = text.split("\n")
    commons = {block["start"]: block for block in common_blocks(text)}
    blocks, start, script, request_started, run_started = [], 0, False, False, False
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if not script and not request_started and run_started and re.match(r'^(?:run|import)\s+', stripped) and i > start:
            blocks.append((start, i))
            start = i
        if not script and ln.startswith("###") and i > start:
            blocks.append((start, i))
            start = i
            request_started = False
            run_started = False
        elif not script and ln.startswith("###"):
            start = i
            request_started = False
            run_started = False
        elif not script and REQ_RX.match(stripped) and not re.match(r'^(?:run|import)\s+', stripped) and not (
                not stripped or stripped.startswith(('#', '//', '@', '<', '>', '='))):
            request_started = True
        if not script and not request_started and re.match(r'^run\s+', stripped):
            run_started = True
        if not script and '{%' in stripped and stripped.startswith(('<', '>', '=')):
            script = '%}' not in stripped[stripped.index('{%') + 2:]
        elif script and '%}' in stripped:
            script = False
    blocks.append((start, len(lines)))
    out, fvars, imports = [], [], []
    for b0, b1 in blocks:
        title = lines[b0][3:].strip() if lines[b0].startswith("###") else ""
        req = {"name": title, "start": b0, "end": b1, "line": None, "method": "GET", "url": "",
               "headers": [], "files": [], "handler": None, "handler_file": None, "pre": None,
               "scripts": [], "subst": [], "saves": [], "redirects": [], "body": "", "no_log": False}
        i = b0 + (1 if lines[b0].startswith("###") else 0)
        # before the request line: comments, @name, file variables, a pre-request script
        while i < b1:
            ln = lines[i].strip()
            m = NAME_RX.match(ln)
            if m:
                req["name"] = req["name"] or m.group(1)
                req['id'] = m.group(1)
            elif IMPORT_RX.match(ln):
                imports.append({'path': IMPORT_RX.match(ln).group(1), 'line': i})
            elif re.fullmatch(r'(?:#|//)\s*@no-log', ln):
                req['no_log'] = True
            elif VAR_RX.match(ln):
                v = VAR_RX.match(ln)
                fvars.append({"line": i, "name": v.group(1), "value": v.group(2)})
            elif i in commons:
                i = commons[i]["end"]
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
                req["redirects"].append({"line": i, "path": SAVE_RX.match(ln).group(1),
                                         "overwrite": ln.startswith(">>!")})
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
    return {"requests": out, "vars": fvars, 'imports': imports, 'common': list(commons.values())}


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


def script_json(value) -> str:
    """JSON embedded in an HTTP script must not contain its `%}` closing delimiter.

    Percent only occurs inside JSON strings, so its JS Unicode escape preserves values
    while preventing data received from an API from terminating the generated handler.
    """
    return json.dumps(value).replace('%', r'\u0025')


def capture_globals(names: list[str], variables: list[str] | None = None, request_index: int | None = None) -> str:
    """Capture scoped runtime data; undefined globals explicitly invalidate stale stored values."""
    def collect(items, getter):
        return ('%s.reduce(function(out, n) {out[n] = %s(n); return out;}, Object.create(null))'
                % (items, getter))
    payload = ('{globals: %s, unset: %s.filter(function(n) {return client.global.get(n) === undefined;}), '
               'iteration: typeof request.iteration === "function" ? request.iteration() : 0, '
               'variables: %s, request_index: %s}' % (collect('names', 'client.global.get'), 'names',
                                   collect(script_json(variables or []), 'request.variables.get'), script_json(request_index)))
    # Tested CLI exposes the current store as vars. Feature-detect it and retain the literal
    # key fallback for engines without it; never monkey-patch setters or insert private globals.
    payload = ('(function(names) { try { Object.keys(client.global.vars || {}).forEach(function(n) {'
               'if (names.indexOf(n) < 0) names.push(n);}); } catch (_) {} return %s; })(%s)'
               % (payload, script_json(names)))
    return ';client.test("%sv1:" + JSON.stringify(%s), function () {});' % (CAPTURE, payload)


def capture_response_log() -> str:
    """The native CLI drops client.log output. Keep native calls and carry bounded response
    messages in private report metadata. Pre-request scripts have no client.test channel.
    Never touch globals/request variables or stringify user objects a second time.
    """
    return (';(function(native) { var left = %d, cut = false; client.log = function(text) {'
            'var result = native.apply(client, arguments); try { if (!cut) {'
            'var line = typeof text === "string" ? text : text === null || '
            '["undefined", "number", "boolean", "bigint"].indexOf(typeof text) >= 0 ? String(text) : "[non-string log message]";'
            'var over = line.length + 1 > left; line = line.slice(0, Math.max(0, left - 1));'
            'left -= line.length + 1; cut = over; client.test(%s + JSON.stringify({text: line, cut: over}), function() {});'
            '} } catch (_) {} return result; }; })(client.log);' % (MAX_SCRIPT_LOG, script_json(LOG_CAPTURE)))


def instrument_rows(rows: list, names: list[str], parsed: dict | None = None,
                    variables: list[str] | None = None, force: bool = False) -> list:
    """(text, original line) rows with one extra client.test per request whose NAME carries the
    values of the globals the file sets (ijhttp prints client.log nowhere; the JUnit report has
    test names). Into an existing handler on its `%}` line (split after a line comment); a request without one
    gets two lines (a blank, `> {% … %}`). A request with a handler file (> ./x.js) is left alone."""
    if not names and not variables and not force:
        return rows
    parsed = parsed or parse("\n".join(t for t, _ in rows))
    rows = list(rows)
    edits = []   # (row, kind) applied bottom-up
    for r in parsed["requests"]:
        code = capture_globals(names, variables, r['index'])
        if r["handler_file"]:
            continue
        if r["handler"]:
            edits.append((r["handler"][1], "into", code))
        else:
            last = r["line"]
            for k in range(r["line"], r["end"]):
                t = rows[k][0].strip()
                if t and not t.startswith("###") and not t.startswith("#") and not t.startswith("//"):
                    last = k
            edits.append((last, "after", code))
    for ln, kind, code in sorted(edits, reverse=True):
        if kind == "into":
            t, o = rows[ln]
            cut = t.rfind("%}")
            if '//' in t[:cut]:
                rows[ln:ln + 1] = [(t[:cut], o), (code + ' ' + t[cut:], None)]
            else:
                rows[ln] = (t[:cut] + code + " " + t[cut:], o)
        else:
            rows[ln + 1:ln + 1] = [("", None), ("> {% " + code + " %}", None)]
    # Reparse after the capture edits so single-line and multiline original handlers have
    # the same prefix; external handlers receive it in their private file below.
    for request in parse('\n'.join(t for t, _ in rows))['requests']:
        if request['handler']:
            ln = request['handler'][0]
            text, original = rows[ln]
            rows[ln] = (text.replace('{%', '{% ' + capture_response_log(), 1), original)
    return rows


def runtime_rows(rows: list, marker: str, names: list[str], variables: list[str], mapping: dict,
                 work: str | None = None, script_maps: dict | None = None, *, barriers: bool = False, native_modules: bool = False,
                 code_context=None) -> list:
    """Instrument the private copy only; native callback/exit semantics and JUnit stay intact."""
    rows = list(rows)
    for request in reversed(parse('\n'.join(t for t, _ in rows))['requests']) if barriers else []:
        if not request['pre'] and not (set(request['scripts']) - {request['handler_file']}):
            rows.insert(request['line'], ('< {% %}', None))
    out = list(rows)
    edits = []
    def bootstrap(request, phase, source, reference=None):
        prefix, suffix = httpruntime.bootstrap(marker, request, phase, names, variables)
        if work and (bool(re.search(r'\b(?:import|export)\b|piper-dynamic-script:', source)) if native_modules else httpdeps.module_dependencies(source)):
            # ES imports execute before the root module body. A first, private
            # dependency installs the channel before user module side effects/tests.
            directory = os.path.join(work, 'runtime')
            os.makedirs(directory, mode=0o700, exist_ok=True)
            path = os.path.join(directory, '%d-%s.js' % (request, phase))
            symbol = suffix[1:suffix.index('(')]
            with open(path, 'w', encoding='utf-8') as module:
                module.write(prefix + '\nexport {' + symbol + ' as state};\n')
            prefix = 'import {state as ' + symbol + '} from ' + script_json(path) + ';'
        return (code_context(request, phase, reference) if code_context else '') + prefix, suffix
    for request in parse('\n'.join(t for t, _ in rows))['requests']:
        spans = [('pre', request['pre']), ('response', request['handler'])]
        for phase, span in spans:
            if not span:
                continue
            start, end = span
            prefix, suffix = bootstrap(request['index'], phase, '\n'.join(line for line, _ in rows[start:end + 1]))
            # Keep original JS at top level (ES module imports cannot be nested in a try).
            text, origin = out[start]
            out[start] = (text.replace('{%', '{% ' + prefix + ' ', 1), origin)
            text, origin = out[end]
            cut = text.rfind('%}')
            out[end] = (text[:cut] + '\n' + suffix + ' ' + text[cut:], origin)
        for phase, reference in [('response', request['handler_file'])] + [
                ('pre', ref) for ref in request['scripts'] if ref != request['handler_file']]:
            if not reference:
                continue
            source = mapping[reference]
            # A handler can occur in several requests; each needs its own stable identity.
            target = os.path.splitext(source)[0] + '-piper-%s-%d.js' % (phase, request['index'])
            with open(source, encoding='utf-8') as original:
                code = original.read()
            prefix, suffix = bootstrap(request['index'], phase, code, source)
            with open(target, 'w', encoding='utf-8') as private:
                private.write(prefix + '\n' + code + '\n' + suffix + '\n')
            if script_maps is not None:
                script_maps[target] = [None] + script_maps[source] + [None, None]
            for i in range(request['start'], request['end']):
                ref = (HANDLER_FILE_RX if phase == 'response' else REF_RX).match(out[i][0].strip())
                if ref and ref.group(1) == reference:
                    edits.append((i, ('> ' if phase == 'response' else '< ') + target))
    for i, text in edits:
        out[i] = (text, out[i][1])
    return [(part, origin) for text, origin in out for part in text.split('\n')]


def restore_globals_rows(rows: list, values: dict) -> tuple[list, str, str]:
    """Initialize the actual store once, before the selected request's own pre-handler.

    Return an external pre-handler reference when its private snapshot needs the prefix.
    No -V override: it would change the environment scope and override later script values.
    """
    if not values:
        return rows, "", ""
    code = ';'.join('client.global.set(%s, %s)' % (script_json(k), script_json(v)) for k, v in values.items()) + ';'
    rows = list(rows)
    first = parse("\n".join(t for t, _ in rows))["requests"][0]
    for i in range(first['start'], first['line']):
        text, original = rows[i]
        if text.strip().startswith('< {%'):
            rows[i] = (text.replace('< {%', '< {% ' + code, 1), original)
            return rows, '', ''
        ref = REF_RX.match(text.strip())
        if ref:
            return rows, ref.group(1), code
    rows.insert(first["line"], ('< {% ' + code + ' %}', None))
    return rows, "", ""


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
        if not 0 <= n < len(linemap):
            return rel
        source = linemap[n]
        return "%s:%d" % (source[0], source[1] + 1) if isinstance(source, tuple) else "%s:%d" % (rel, source + 1)
    msg = re.sub(re.escape(run_file) + r":(\d+)", sub, msg)
    return msg.replace(run_file, rel).replace(os.path.dirname(run_file) + "/", "")


def relink_scripts(message: str, maps: dict) -> str:
    """Map private script copies and generated prefixes to their authored source lines."""
    for path, origins in sorted(maps.items(), key=lambda item: -len(item[0])):
        owner = next((origin[0] for origin in reversed(origins) if origin is not None), None)
        if owner is None:
            continue
        def replace(match):
            index = int(match[1]) - 1
            if 0 <= index < len(origins) and origins[index] is not None:
                source = origins[index]
                return '%s:%d' % (source[0], source[1] + 1)
            return owner
        message = re.sub(re.escape(path) + r':(\d+)', replace, message).replace(path, owner)
    return message


def rewrite_refs(text: str, mapping: dict, generated: list[dict] | None = None) -> str:
    """`< ./x` and `> ./x.js` lines pointing at the copies piperd made (the run's snapshot)."""
    if generated is not None:
        lines = text.split("\n")
        for r, outputs in zip(parse(text)["requests"], generated):
            local = dict(mapping, **outputs)
            for i in range(r["start"], r["end"]):
                lines[i] = rewrite_refs(lines[i], local)
        return "\n".join(lines)
    out = []
    for ln in text.split("\n"):
        s = ln.strip()
        m = None if SAVE_RX.match(s) else (REF_RX.match(s) or HANDLER_FILE_RX.match(s))
        if m and m.group(1) in mapping:
            ln = ln[:ln.index(m.group(1))] + mapping[m.group(1)] + ln[ln.index(m.group(1)) + len(m.group(1)):]
            # ijhttp already templates ordinary file inputs; `<@` is another client's syntax
            # and is otherwise sent literally. Change only the private run's reference line.
            ln = re.sub(r"^(\s*)<@(?=\s)", r"\1<", ln)
        out.append(ln)
    return "\n".join(out)


def redirect_rows(rows: list, work: str) -> tuple[list, list[dict]]:
    """Stage each native response by its request and collection iteration identity.

    A random private request variable is set AFTER the authored pre-handler on each
    native iteration. The null discovery pass and ordinary requests retain the old
    stage name, so ordinary export-to-input chains keep working unchanged.
    """
    out, redirects = list(rows), []
    insertions = []
    for r in parse("\n".join(t for t, _ in rows))["requests"]:
        if not r['redirects']:
            continue
        variable = '__piper_stage_' + _secrets.token_hex(16)
        code = 'request.variables.set(' + script_json(variable) + ', request.iteration() === null || request.iteration() === 0 ? "" : "-" + request.iteration());'
        if r['pre']:
            end = r['pre'][1]
            text, origin = out[end]
            cut = text.rfind('%}')
            out[end] = (text[:cut] + '\n' + code + '\n' + text[cut:], origin)
        else:
            pre_files = [ref for ref in r['scripts'] if ref != r['handler_file']]
            if pre_files:
                # runtime_rows already made one private file per request occurrence.
                path = pre_files[0]
                if os.path.commonpath((os.path.realpath(path), os.path.realpath(work))) != os.path.realpath(work):
                    raise HttpError('private pre-handler is outside the run snapshot')
                with open(path, 'a', encoding='utf-8') as script:
                    script.write('\n' + code + '\n')
            else:
                insertions.append((r['line'], ('< {% ' + code + ' %}', None)))
        for d in r["redirects"]:
            target = os.path.join(work, "staged", "response-%d" % len(redirects))
            redirects.append(dict(d, request=r["index"], staged=target, stage_root=work, iterations=True))
            out[d["line"]] = (">>! " + target + '{{' + variable + '}}', rows[d["line"]][1])
    for position, row in reversed(insertions):
        out.insert(position, row)
    return [(part, origin) for text, origin in out for part in text.split('\n')], redirects


def complete_report(responses: list[dict], reports: list[dict], count: int) -> bool:
    """Exports require one unambiguous successful report for every native occurrence."""
    if len(responses) != len(reports) or not responses or any(not row['ok'] for row in responses):
        return False
    groups = {index: [] for index in range(count)}
    for row in responses:
        index = row.get('request_index')
        if type(index) is not int or index not in groups:
            return False
        iteration = row.get('iteration')
        if iteration is not None and (type(iteration) is not int or iteration < 0):
            return False
        groups[index].append(iteration)
    return all(values == [None] or (bool(values) and None not in values and sorted(values) == list(range(len(values))))
               for values in groups.values())


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


def normalize_env(doc: dict, environments: list[str] | None = None) -> dict:
    """Materialize $shared defaults once for the runner, previews and security checks.

    Environment entries override whole shared keys, as in the source dialect. Transport
    settings must stay explicit per environment: a shared variable block cannot declare
    a tunnel or disable certificate verification.
    """
    shared = doc.get("$shared", {})
    if not isinstance(shared, dict):
        raise HttpError("$shared must be an object")
    if META_KEY in shared or SSL_KEY in shared:
        raise HttpError("$shared cannot contain transport settings; declare them per environment")
    out = {}
    for name in environments if environments is not None else doc:
        if name.startswith("$"):
            continue
        if name not in doc and not shared:
            continue
        value = doc.get(name, {})
        if not isinstance(value, dict):
            raise HttpError("an HTTP environment must be an object")
        out[name] = dict(shared, **value)
    return out


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
    def auth_endpoint(key, value):
        # A plain authentication endpoint is approval metadata, not a token.
        # URLs with userinfo/query/fragment stay secret; so do credential fields
        # whose value happens to look like a URL.
        return (key.startswith('Security.Auth.') and key.rsplit('.', 1)[-1] in
                ('Token URL', 'Auth URL', 'Device Auth URL', 'Redirect URL') and is_locator(value))
    vals = {v for k, v in leaves(private).items() if not auth_endpoint(k, v)}
    vals |= {v for k, v in leaves(public).items()
             if not auth_endpoint(k, v) and (not is_locator(v) if public_hidden else SECRET_KEY_RX.search(k.rsplit(".", 1)[-1]))}
    return sorted((v for v in vals if len(v) >= 4), key=len, reverse=True)


def redactor(secret_list: list[str]):
    """text -> text with each secret masked, also base64 / URL-encoded, and any base64 token
    that decodes to something containing one (Basic user:pass)."""
    # Cookies and managed tokens can arrive after the run's approval. Existing
    # preview callbacks must mask those values too, including their encodings.
    known, variants, prefixes = None, [], {}

    def b64_hit(m: re.Match) -> str:
        tok = m.group(0)
        try:
            raw = base64.b64decode(tok.replace("-", "+").replace("_", "/") + "=" * (-len(tok) % 4), validate=False)
            dec = raw.decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return tok
        return MASK if any(s and s in dec for s in secret_list) else tok

    def scrub(text: str, *, cut=False) -> str:
        nonlocal known, variants
        if not text or not secret_list:
            return text or ""
        current = tuple(secret_list)
        if current != known:
            encoded = set()
            for s in current:
                b = s.encode()
                encoded |= {s, urllib.parse.quote(s, safe=""), urllib.parse.quote_plus(s),
                            base64.b64encode(b).decode().rstrip("="), base64.urlsafe_b64encode(b).decode().rstrip("=")}
            variants = sorted((v for v in encoded if v), key=len, reverse=True)
            prefixes.clear()
            known = current
        if cut:
            # The native logger can hit its own bound halfway through a token.
            # Match the longest suffix/prefix in linear time, before replacement.
            suffix = text.rstrip('\n')
            for value in variants:
                if len(value) < 4:
                    continue
                if value not in prefixes:
                    table, matched = [0] * len(value), 0
                    for i in range(1, len(value)):
                        while matched and value[i] != value[matched]:
                            matched = table[matched - 1]
                        matched += value[i] == value[matched]
                        table[i] = matched
                    prefixes[value] = table
                table, matched = prefixes[value], 0
                for char in suffix[-len(value):]:
                    if matched == len(value):
                        matched = table[matched - 1]
                    while matched and char != value[matched]:
                        matched = table[matched - 1]
                    matched += char == value[matched]
                if matched >= 4:
                    suffix = suffix[:-matched] + MASK
            text = suffix + text[len(text.rstrip('\n')):]
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
    if 'Cannot run program' in error and ('posix_spawn' in error or 'error=1' in error or 'error=13' in error):
        return 'HTTP scripts cannot start child processes in OS isolation. ' + error
    if sandbox and re.search(r"refused|reset|closed|end of (stream|file)|EOF|timed? ?out|terminated", error, re.I):
        return error + (" - hint: the request went into %s through Piper's relay - is the service still up there "
                        "and listening on that port?" % sandbox)
    if re.search(r'certificate_required|bad_certificate|empty client certificate chain', error, re.I):
        return error + (' - hint: the server requires a valid client TLS identity; configure clientCertificate and '
                        'clientCertificateKey in SSLConfiguration of the private HTTP environment')
    if re.search(r"PKIX path|unable to find valid certification path|SSLHandshakeException|CERTIFICATE_VERIFY_FAILED"
                 r"|No subject alternative|certificate", error, re.I):
        return error + (" - hint: the server's certificate is self-signed or not for this name: add \"%s\": "
                        "{\"verifyHostCertificate\": false} to this environment in http-client/%s (IDEA honours it; "
                        "Piper only from an env file hidden from sessions - the user edits it)" % (SSL_KEY, ENV_FILE))
    h = url_host(url) or ""
    if h in ("localhost", "127.0.0.1") and re.search(r"Connection refused|ConnectException|connection refused", error):
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


def relay_response_head(head: bytes, native: bool = False) -> bytes:
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
    if not native and ct and not is_text(ct) and (clen is None or clen <= RELAY_TEXT_MAX):
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

    def __init__(self, argv: list[str], *, native: bool = False):
        import socket
        self.argv, self.procs, self.closed, self.native = argv, [], False, native
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

        req, resp = RelayHeadFilter(relay_request_head, False), RelayHeadFilter(lambda head: relay_response_head(head, self.native), True)

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
            if v and v in raw:
                return True
    text = raw.decode("latin-1")   # a base64 token that decodes to something holding a secret (Basic user:pass)
    for m in B64_TOKEN_RX.finditer(text):
        tok = m.group(0)
        try:
            dec = base64.b64decode(tok.replace("-", "+").replace("_", "/") + "=" * (-len(tok) % 4)).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            continue
        if any(x and x in dec for x in secret_list):
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
        elif cur.get('binary') and ln.startswith('Response code:'):
            match = re.match(r'^Response code: (\d+).*?Time: (\d+)ms', ln)
            if match:
                cur['time_ms'] = int(match[2])
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
        elif name.startswith(LOG_CAPTURE):
            try:
                entry = json.loads(name[len(LOG_CAPTURE):])
                if isinstance(entry, dict) and isinstance(entry.get('text'), str):
                    remaining = MAX_SCRIPT_LOG - len(cur.get('script_log', ''))
                    line = entry['text'] + '\n'
                    cur['script_log'] = cur.get('script_log', '') + line[:max(0, remaining)]
                    cur['script_log_cut'] = cur.get('script_log_cut', False) or bool(entry.get('cut')) or len(line) > remaining
            except ValueError:
                pass
        elif name.startswith(CAPTURE):
            try:
                value = name[len(CAPTURE):]
                if value.startswith('v1:'):
                    data = json.loads(value[3:])
                    if isinstance(data, dict) and isinstance(data.get('globals'), dict):
                        cur['globals'] = data['globals']
                        cur['captured'] = True
                        cur['iteration'] = data.get('iteration', 0)
                        cur['request_index'] = data.get('request_index')
                        unset = data.get('unset')
                        cur['unset'] = [k for k in unset if isinstance(k, str)] if isinstance(unset, list) else []
                        cur['variables'] = data.get('variables') if isinstance(data.get('variables'), dict) else {}
                else:   # old stored/fixture reports
                    g = json.loads(value)
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
        r = dict(c, tests=j["tests"], globals=j["globals"], unset=j.get('unset', []), variables=j.get('variables', {}),
                 captured=j.get('captured', False), iteration=j.get('iteration', 0), request_index=j.get('request_index'))
        if j.get('script_log'):
            r.update(script_log=j['script_log'], script_log_cut=j.get('script_log_cut', False))
        if j["error"] and not r["error"]:
            r["error"] = j["error"].split("\n")[0][:500]
        r["ok"] = not r["error"] and all(t["ok"] for t in r["tests"])
        out.append(r)
    return out


def missing_results(rows: list[dict], events: list[dict], requests: list[dict], values: dict) -> list[dict]:
    """Show selected requests omitted by an aborted native pre-script/report writer.

    A missing result proves neither transmission nor success. Retain existing native
    test outcomes, and associate received responses only using unambiguous identities.
    """
    identities = list(dict.fromkeys((event['request'], event['iteration']) for event in events
                                   if event['kind'] == 'identity' and event['phase'] == 'response'))
    received = [row for row in rows if row.get('status') is not None]
    if len(identities) == len(received):
        for row, (index, iteration) in zip(received, identities):
            if row.get('request_index') is None:
                row.update(request_index=index, iteration=iteration)
    if any(type(row.get('request_index')) is not int for row in rows):
        return rows  # Ambiguous native rows are never assigned by their names/order.
    covered = {row['request_index'] for row in rows}
    for index, request in enumerate(requests):
        if index not in covered:
            rows.append(dict(name=request['name'], method=request['method'], url=substitute(request['url'], values),
                req_headers=[], status=None, status_text='', headers=[], body='', time_ms=None, tests=[], globals={},
                ok=False, request_index=index, iteration=None,
                error='native run stopped without a result for this request; transmission and test results are unconfirmed; not retried'))
    return rows


def runtime_stopped(rows: list[dict], events: list[dict], requests: list[dict], position, values: dict) -> list[dict]:
    """Keep received responses; unfinished converter placeholders are not extra requests.

    The JVM cannot finish JUnit after halt87. Only response identity frames may
    associate actual responses; missing earlier reports stay explicitly unknown.
    """
    received = [row for row in rows if row.get('status') is not None]
    identities = list(dict.fromkeys((event['request'], event['iteration']) for event in events
                                   if event['kind'] == 'identity' and event['phase'] == 'response'))
    exact = len(identities) == len(received)
    for index, response in enumerate(received):
        response.update(ok=False, error='native run stopped at a runtime input; earlier requests may have run; not retried')
        response['tests'] = [{'name':'Native report incomplete', 'ok':False,
                             'message':'HTTP response received, but final native test results are unavailable after the run stopped'}]
        response.update(request_index=identities[index][0] if exact else None,
                        iteration=identities[index][1] if exact else None)
    known = type(position) is int and 0 <= position < len(requests)
    covered = {row.get('request_index') for row in received}
    for index, request in enumerate(requests):
        if index in covered:
            continue
        if not known or index < position:
            error = 'native report incomplete; this earlier request may have run'
        else:
            error = 'runtime input refused or not approved' if index == position else 'not sent after the runtime input stopped the run'
        received.append(dict(name=request['name'], method=request['method'], url=substitute(request['url'], values),
            req_headers=[], status=None, status_text='', headers=[], body='', time_ms=None, tests=[], globals={},
            ok=False, request_index=index, iteration=None, error=error + '; earlier requests may have run; not retried'))
    return received


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


def run_targets(requests: list, values: dict) -> list[tuple[str, int]]:
    """The network broker grants only destinations known before native code runs."""
    targets = set()
    for request in requests:
        url = substitute(request['url'], values)
        if '://' not in url:
            url = 'http://' + url
        try:
            parsed = urllib.parse.urlsplit(url)
            host, port = parsed.hostname, parsed.port
            if (parsed.scheme not in ('http', 'https') or not host or VAR_USE_RX.search(host)
                    or port is not None and not 1 <= port <= 65535):
                raise ValueError('unresolved destination')
            targets.add((httpjail.host_name(host), port if port is not None else (443 if parsed.scheme == 'https' else 80)))
        except ValueError:
            raise HttpError('every HTTP destination must be known before the isolated run; no request was sent') from None
    return sorted(targets)


def run_ijhttp(binary: str, workdir: str, http_file: str, env_name: str, env_dir: str | None,
               variables: dict, insecure: bool, timeout: int, *, connect_timeout_ms: int = 5000,
               socket_timeout_ms: int = 60000, targets=(), network_failures=None, capabilities=None, on_stdout=None) -> tuple[int, str, str]:
    """(rc, console, junit xml) of one run; the console is stdout+stderr."""
    report = os.path.join(workdir, "report")
    argv = [binary, "--no-progress", "-L", "VERBOSE", "-r", report,
            "--connect-timeout", str(connect_timeout_ms), "--socket-timeout", str(socket_timeout_ms)]
    if env_name:
        argv += ["-e", env_name]
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
        proxy = httpjail.Egress(targets, timeout, unix=sys.platform.startswith('linux'),
                                connect_timeout=connect_timeout_ms / 1000)
        try:
            argv[-1:-1] = ['--proxy', 'socks://127.0.0.1:%d' % proxy.port]
            r = httpjail.run(binary, workdir, argv[1:], env, timeout, proxy.port, proxy_path=proxy.unix_path, on_stdout=on_stdout)
            if capabilities is not None:
                capabilities['binary_safe'] = getattr(r, 'http_binary_safe', False)
            if r.returncode == 86 and getattr(r, 'http_binary_safe', False):
                raise HttpError('Binary response exceeds 64 MiB; requests may already have run; not retried or saved')
            if r.returncode == 89 and getattr(r, 'http_binary_safe', False):
                raise HttpError('HTTP cookie broker refused an invalid update or destination; earlier requests may have run; not retried')
        finally:
            proxy.close()
            if network_failures is not None:
                with proxy.lock:
                    network_failures.update({target: reason for target, reason in proxy.failures.items()
                                             if target not in proxy.connected})
    except subprocess.TimeoutExpired:
        raise HttpError("ijhttp did not finish in %d s" % timeout) from None
    except httpjail.OutputLimit as e:
        raise HttpError(str(e)) from None
    except (OSError, httpjail.IsolationError) as e:
        raise HttpError("ijhttp could not start safely: %s" % e) from None
    console = (r.stdout + b"\n" + r.stderr).decode("utf-8", "replace")
    xml = ""
    rp = os.path.join(report, "report.xml")
    # the launcher never got to run (a failed request exits non-zero too, with a report; a test's fake needs no Java)
    if r.returncode and not os.path.isfile(rp) and java_missing(env):
        raise HttpError("ijhttp needs Java 17 or newer and there is none on piperd's PATH (Linux / WSL: "
                        "sudo apt install openjdk-17-jre-headless, then task piper:install-service)")
    if os.path.lexists(rp):
        try:
            data = common.safe_read(workdir, 'report/report.xml', (64 << 20) + 1, single_link=True)
        except (OSError, ValueError):
            raise HttpError('ijhttp produced an unsafe report path; the request may already have run') from None
        if len(data) > 64 << 20:
            raise HttpError('ijhttp report exceeds 64 MiB; the request may already have run')
        xml = data.decode('utf-8', 'replace')
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
        policy = dict(DEFAULTS, **(self.policy.get("http") or {}))
        for name in ('connect_timeout_ms', 'socket_timeout_ms'):
            value = policy[name]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3600000:
                raise HttpError('%s must be an integer between 1 and 3600000 milliseconds' % name)
        return policy

    def http_status(self) -> dict:
        b = find_ijhttp(self._http_policy())
        return {"ijhttp": b, "installing": self._http_install["state"] == "running",
                "install_error": self._http_install["error"]}

    def _http_lock(self, key: str) -> threading.Lock:
        with self._http_locks_lock:
            return self._http_locks.setdefault(key, threading.Lock())

    @staticmethod
    def _http_read(root: str, rel: str) -> str:
        if not rel.lower().endswith((".http", ".rest")):
            raise HttpError("not a .http file: %s" % rel)
        try:
            raw = common.safe_read(root, rel, MAX_FILE + 1)
            if len(raw) > MAX_FILE:
                raise HttpError('%s is larger than 1 MiB; no request was sent' % rel)
            return raw.decode("utf-8", "replace")
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
                raw = common.safe_read(root, rel, MAX_FILE + 1)
                if len(raw) > MAX_FILE:
                    raise HttpError('%s is larger than 1 MiB; no request was sent' % rel)
                doc = json.loads(raw.decode("utf-8") or "{}")
            except FileNotFoundError:
                doc = {}
            except (OSError, ValueError) as e:
                raise HttpError("cannot read %s: %s" % (rel, e)) from None
            if not isinstance(doc, dict):
                raise HttpError("%s must contain an object" % rel)
            out.append(doc)
        names = list(dict.fromkeys(list(out[0]) + list(out[1])))
        return normalize_env(out[0], names), normalize_env(out[1], names)

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
    def _refs(self, ctx: dict, rel: str, parsed: dict, agent: bool, values: dict | None = None,
              runtime: list | None = None, runtime_code: list | None = None) -> dict:
        """Referenced files (`< x`, `> x.js`): project-relative path per reference as written."""
        base = os.path.dirname(rel)
        root = os.path.realpath(ctx['root'])
        path_values = dict(values or {}, **{'$projectRoot': root,
                                           '$historyFolder': os.path.join(root, '.idea', 'httpRequests')})
        out = {}
        scripts_before = False
        earlier_outputs = set()
        reserved_outputs = set()
        dynamic_output = False
        for r in parsed["requests"]:
            scripts_before |= bool(r['pre'] or len(r['scripts']) > int(bool(r['handler_file'])))
            for ref in r["files"]:
                dynamic = scripts_before and any(not m.group(1).startswith('$') for m in VAR_USE_RX.finditer(ref))
                unresolved = VAR_USE_RX.search(substitute(ref, path_values))
                resolved = substitute(ref, path_values)
                relative = os.path.normpath(os.path.join(os.path.dirname(r.get('owner', rel)), resolved)) if not os.path.isabs(resolved) else os.path.relpath(resolved, root)
                if runtime_code is not None and ref in r['scripts'] and (dynamic or unresolved or r.get('runtime_inputs') or dynamic_output or relative in earlier_outputs):
                    runtime_code.append(dict(request=r['index'], owner=r.get('owner', rel), reference=ref,
                        phase='response' if ref == r['handler_file'] else 'pre', marker='/*piper-dynamic-script:%s*/' % _secrets.token_hex(16)))
                    continue
                if runtime is not None and ref not in r['scripts'] and (dynamic or unresolved or r.get('runtime_inputs')):
                    runtime.append(dict(request=r['index'], owner=r.get('owner', rel), reference=ref))
                    continue
                if dynamic:
                    raise HttpError('%s: variable file paths after scripts need runtime dependency handling; '
                                    'no request was sent' % ref)
                if ref in out:
                    continue
                path = substitute(ref, path_values)
                if VAR_USE_RX.search(path):
                    raise HttpError('%s: a file path must be known before the run; no request was sent' % ref)
                if path.startswith('~'):
                    raise HttpError('%s: use a path inside the project' % ref)
                try:
                    p = self._norm(os.path.relpath(path, root) if os.path.isabs(path) else
                                   os.path.normpath(os.path.join(base, path)))
                except HttpError:
                    raise HttpError("%s: a referenced file must stay inside the project" % ref) from None
                if agent and ctx["hidden"](p):
                    raise HttpError("%s is hidden by the isolation policy - a session cannot send it" % ref)
                out[ref] = p
            scripts_before |= bool(r['handler'] or r['handler_file'])
            for directive in r['redirects']:
                resolved = substitute(directive['path'], path_values)
                if not VAR_USE_RX.search(resolved):
                    target = os.path.normpath(os.path.join(os.path.dirname(r.get('owner', rel)), resolved)) if not os.path.isabs(resolved) else os.path.relpath(resolved, root)
                    actual = self._redirect_name(root, target, directive['overwrite'], reserved_outputs)
                    earlier_outputs.update((target, actual))
                    reserved_outputs.add(actual)
                else:
                    dynamic_output = True
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
    def _save_target(self, ctx: dict, save_to, agent: bool = True, exact: bool = False) -> str:
        """The project file a response goes to: a bare name lands in development/; never a dot path,
        never a file git tracks, never a .http / env file. A path the isolation policy hides: never for
        a session; the GUI's run may (L3b, 2026-10-07 - the one place an unmasked response is safe), not
        while the policy cannot be read (everything looks hidden then)."""
        raw = str(save_to or "").strip()
        rel = self._norm(raw if exact or "/" in raw.strip("/") else SAVE_DIR + "/" + raw)
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

    @staticmethod
    def _redirect_name(root: str, rel: str, overwrite: bool, reserved: set | None = None) -> str:
        base, ext = os.path.splitext(rel)
        n = 0
        while not overwrite and (os.path.lexists(os.path.join(root, rel)) or rel in (reserved or ())):
            n += 1
            rel = "%s-%d%s" % (base, n, ext)
        return rel

    def _redirect_target(self, ctx: dict, rel: str, path: str, values: dict, agent: bool,
                         overwrite: bool = True, secret_list: list[str] | None = None) -> str:
        """IDEA redirects resolve relative to the original .http file, never its temporary copy."""
        path = substitute(path, dict(values, **{"$projectRoot": os.path.realpath(ctx["root"]),
                                               "$historyFolder": os.path.join(ctx["root"], ".idea", "httpRequests")}))
        if redactor(secret_list or [])(path) != path:
            raise HttpError("a response file path must not contain environment secrets")
        if VAR_USE_RX.search(path):
            raise HttpError("response path has unresolved variables: %s" % path)
        if os.path.isabs(path):
            path = os.path.relpath(path, os.path.realpath(ctx["root"]))
        else:
            path = os.path.join(os.path.dirname(rel), path)
        path = os.path.normpath(path)
        if path == ".." or path.startswith("../") or path.startswith("~"):
            raise HttpError("a response file must stay inside the project")
        path = self._norm(path)
        self._save_target(ctx, self._redirect_name(ctx["root"], path, overwrite), agent, exact=True)
        return path

    def _save_response(self, ctx: dict, rel: str, row: dict, raw: bytes, secret_list: list[str],
                       agent: bool = True, redirect: bool | None = None) -> dict:
        """Write a response into the project. The session reads the file unscrubbed, so a body holding a
        secret of the environment is written masked (text) or not at all (binary). The GUI's run into a
        file hidden from Claude (L3b): as received - an existing one rewritten IN PLACE (a rename would
        detach a live sandbox's over-mount, P0), a new one only while no sandbox of the tree runs
        (workbench._ops_guard), its temp file outside the project."""
        root = ctx["root"]
        with self._write_lock:   # the check and the write as one step, like workbench.fs_write / fs_create
            if redirect is not None:
                rel = self._redirect_name(root, rel, redirect)
            # Both save_to and authored redirects may have waited for an
            # approval/network response. Recheck the final path at publication.
            self._save_target(ctx, rel, agent, exact=True)
            hidden = not agent and bool(ctx["hidden"](rel)) and not getattr(ctx["hidden"], "unreadable", "")
            ctype = next((v for k, v in row["headers"] if k.lower() == "content-type"), "")
            data, note = raw, ""
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

    def _publish_redirects(self, ctx: dict, rel: str, redirects: list[dict], reqs: list[dict],
                           values: dict, secret_list: list[str], agent: bool, file_values: dict | None = None,
                           global_values: dict | None = None, *, binary_safe: bool = False, native_targets: bool = False) -> None:
        """Publish only responses actually produced, never repeat a network request to save it."""
        state = dict(global_values or {})
        for i, row in enumerate(reqs):
            for k in row.get('unset', []):
                state.pop(k, None)
            state.update(row.get("globals") or {})
            current = dict(values, **flatten(state))
            current.update(file_values or {})
            current.update(flatten(row.get('variables') or {}))
            path_secrets = secret_list + cookie_values(reqs) + secret_values(state, {}) + secret_values(row.get('variables') or {}, {})
            for d in (x for x in redirects if x["request"] == row.get("request_index", i)):
                staged = d["staged"]
                iteration = row.get("iteration")
                if d.get("iterations") and iteration not in (None, 0):
                    if type(iteration) is not int or iteration < 0:
                        row.update(ok=False, error="invalid response iteration; not published")
                        continue
                    staged += "-" + str(iteration)
                try:
                    if not os.path.isfile(staged):
                        if row.get("status") is not None:
                            raise HttpError("ijhttp did not produce the response file")
                        continue
                    ctype = next((v for k, v in row["headers"] if k.lower() == "content-type"), "")
                    if not binary_safe and (row.get("binary") or not ctype or not is_text(ctype)):
                        raise HttpError("native binary response export is not byte-safe in the installed CLI; not published")
                    scoped = dict(current, **d.get('file_values', {}))
                    scoped.update(flatten(row.get('variables') or {}))
                    path = d['path']
                    stage_root = d.get('stage_root') or os.path.dirname(staged)
                    if native_targets:
                        metadata = json.loads(common.safe_read(stage_root, os.path.relpath(staged + '.target.json', stage_root),
                                                              32 << 10, single_link=True))
                        if not isinstance(metadata, dict) or set(metadata) != {'path'} or not isinstance(metadata['path'], str):
                            raise HttpError('invalid native response path; not published')
                        path = metadata['path']
                        # Preserve native values already resolved before the response.
                        # A previously undefined variable may still be supplied by its
                        # response handler, as supported by earlier Piper versions.
                        if not VAR_USE_RX.search(path):
                            scoped = {}
                    target = self._redirect_target(ctx, d.get('owner', rel), path, scoped, agent, d["overwrite"],
                                                   path_secrets)
                    raw = common.safe_read(stage_root, os.path.relpath(staged, stage_root), MAX_SAVE + 1, single_link=True)
                    if len(raw) > MAX_SAVE:
                        raise HttpError("the response is larger than %d MB - not saved" % (MAX_SAVE >> 20))
                    row.update(self._save_response(ctx, target, row, raw, path_secrets,
                                                   agent, redirect=d["overwrite"]))
                    row["body_size"] = len(raw)
                except (OSError, ValueError) as e:
                    row["ok"] = False
                    row["error"] = "response not saved: %s" % e

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

    def _globals_key(self, key: str, rel: str, env: str, *, root: str | None = None,
                     env_dir: str | None = None) -> str:
        """Share native state across files only within one physical project/env source."""
        if root is None:
            root = self._http_gui_ctx(key)['root']
            env_dir = self._env_dir_for(root, rel, self._env_dirs(root)) if env else None
        scope = [os.path.realpath(root), env_dir if env_dir is not None else None, env]
        return 'v2:' + hashlib.sha256(json.dumps(scope, ensure_ascii=True).encode()).hexdigest()

    def _execute(self, ctx: dict, rel: str, env: str, index: int | None, agent: bool,
                 approve=None, include_body: bool = False, expect_sha: str = "", save_to: str = "",
                 text: str | None = None, dependency_shas: dict | None = None) -> dict:
        # Serialize the state read, approval, native execution AND state publication.
        # Otherwise a queued file could restore a stale authentication/global snapshot.
        lock = self._http_lock(ctx['key'])
        if not lock.acquire(timeout=LOCK_WAIT_AGENT if agent else -1):
            raise HttpError("another .http run of this project is in progress - try again in a minute")
        try:
            return self._execute_locked(ctx, rel, env, index, agent, approve, include_body,
                                        expect_sha, save_to, text, dependency_shas)
        finally:
            lock.release()

    def _http_approve_runtime_code(self, ctx, info, env, cls, rid, agent, scrub, deadline):
        """Show the exact executable graph once, for both agent and user GUI runs."""
        remaining = deadline - time.monotonic() - 5
        if remaining <= 0:
            raise HttpError('runtime code not asked: less than 5 s of the run deadline left')
        # Filenames themselves may contain an authenticated value. They are
        # display metadata here; the broker keeps the original frozen paths.
        info = dict(info, path=scrub(info['path']), site=scrub(info['site']),
                    deps=[dict(item, path=scrub(item['path'])) for item in info['deps']],
                    origin={key:scrub(value) if isinstance(value, str) else value for key, value in info['origin'].items()})
        env = scrub(env)
        origin = info['origin']
        phase = info['phase']
        preview = httpbroker.preview(info['data'], scrub)
        if preview['preview'] is None:
            raise HttpError('runtime code is not UTF-8 text; not shown or run')
        number = origin['index'] + 1
        when = 'before request #%d' % number if phase == 'pre' else 'in the response handler of request #%d (already sent)' % number
        deps = [dict(file=item['path'], bytes=item['size'], sha256=item['sha']) for item in info['deps']]
        lines = ['⚠ Allow runs this code %s on %s · earlier requests of this run may have run' % (when, cls),
                 "⚠ It can read this run's environment secrets and authenticated responses and change every later request",
                 'Code %s for %s' % (info['path'], origin['file']),
                 '%d bytes · sha256 %s' % (info['size'], info['sha']),
                 'Found at run time: %s in %s · environment %r · this code, this run only' % (info['trigger'], info['site'], env),
                 '%d files · %d bytes with its imports - Piper loads exactly these; a computed import inside them asks again' %
                 (info['files'], info['bytes'])]
        lines.extend('  %s · %d bytes · sha256 %s' % (item['file'], item['bytes'], item['sha256']) for item in deps[:20])
        if len(deps) > 20:
            lines.append('  … and %d more imports, each with its sha256 under Imports on the card' % (len(deps) - 20))
        lines.append('Waiting counts against the run deadline. Deny or no answer stops the run here; nothing is replayed.')
        payload = dict(by='agent' if agent else 'gui', run=rid, project=ctx['key'], file=scrub(ctx['rel']), trigger=info['trigger'], phase=phase,
            entry=dict(file=info['path'], bytes=info['size'], sha256=info['sha']), deps=deps,
            files=info['files'], bytes=info['bytes'], site=dict(file=info['site']), origin=origin,
            env=env, cls=cls, danger=cls == 'prod', deadline=time.time() + remaining,
            preview=preview['preview'], preview_note=preview['preview_note'])
        if agent:
            ok = self.ask('http_runtime_code', ctx['session'], '\n'.join(lines), payload, timeout=remaining)
        else:
            project = self.gui_project(ctx['key'])
            ok = self.ask_gui('http_runtime_code', project.get('title') or project['key'], '\n'.join(lines), payload, remaining)
        if not ok:
            raise HttpError('runtime code declined or timed out; earlier requests may have run; not retried')

    def _execute_locked(self, ctx: dict, rel: str, env: str, index: int | None, agent: bool,
                 approve=None, include_body: bool = False, expect_sha: str = "", save_to: str = "",
                 text: str | None = None, dependency_shas: dict | None = None) -> dict:
        """Parse, check, (approve), snapshot and run; the stored record (full, for the GUI).
        save_to: request `index` is sent by piperd itself and its response written to that project
        file (ijhttp's console cannot carry a binary body)."""
        root, key = ctx["root"], ctx["key"]
        ctx = dict(ctx, rel=rel)
        binary = find_ijhttp(self._http_policy())
        native_compatible = httpcompat.compatible(binary)
        if not binary and not save_to:
            raise HttpError("ijhttp is not installed on the host - Piper GUI: open a .http file and click "
                            "Install, or put `ijhttp` on piperd's PATH")
        if text is None:   # a session may send the file's text instead (written in its own scratch dir)
            text = self._http_read(root, rel)
        source_sha = hashlib.sha256(text.encode()).hexdigest()
        if len(text.encode('utf-8')) > MAX_FILE:
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
        env_dir = self._env_dir_for(root, rel, dirs) if env else None
        public, private = self._load_env(root, env_dir)
        if env and env not in public and env not in private:
            raise HttpError("unknown environment %r - %s offers: %s" % (
                env, (env_dir or ".") + "/" + ENV_FILE,
                ", ".join(e["name"] for e in self._env_list(key, public, private)) or "none"))
        cls = self._env_class(key, env)
        pub_hidden = bool(public) and ctx["hidden"]((env_dir + "/" if env_dir else "") + ENV_FILE)
        private_hidden = bool(private.get(env)) and ctx["hidden"]((env_dir + "/" if env_dir else "") + PRIVATE_FILE)
        secret_list = secret_values(public.get(env) or {}, private.get(env) or {}, pub_hidden)
        gstore = read_json(GLOBALS_FILE, {})
        state_key = self._globals_key(key, rel, env, root=root, env_dir=env_dir)
        jar = None
        legacy_state_key = "\0".join((key, rel, env))
        legacy_allowed = False
        if state_key not in gstore:
            try:
                legacy_allowed = os.path.realpath(self.gui_project(key)['path']) == os.path.realpath(root)
            except (KeyError, ValueError):
                pass
        previous_globals = dict(gstore.get(state_key, gstore.get(legacy_state_key) if legacy_allowed else None) or {})
        gl = dict(previous_globals)
        secret_list += secret_values(gl, {})
        environment_values = dict(flatten(public.get(env) or {}), **flatten(private.get(env) or {}))
        values = dict(environment_values)
        values.update(flatten(gl))
        file_values = {}
        for v in parsed['vars']:
            file_values[v['name']] = substitute(v['value'], dict(values, **file_values))
        values.update(file_values)
        sel = parsed["requests"] if index is None else [parsed["requests"][index]]
        graph = None
        if any(r['method'].lower() in ('run', 'import') for r in sel):
            try:
                graph = httpgraph.Graph(root, rel, text, index, dict(environment_values, **flatten(gl)),
                                       ctx['hidden'] if agent else lambda _: False, parse, substitute)
                if dependency_shas is not None:
                    stamps = graph.stamps()
                    if not isinstance(dependency_shas, dict) or any(
                            stamps.get(str(request['index'])) != dependency_shas.get(str(request['index']))
                            for request in sel if request['method'].lower() == 'run'):
                        raise HttpError('HTTP imports changed since the preview; reopen the file and review the requests again')
                text, parsed = graph.flatten()
                sel = parsed['requests']
                if graph.no_log and sel:
                    sel[0]['no_log'] = True
                file_values = {}
                values = dict(environment_values, **flatten(gl))
            except (OSError, ValueError) as error:
                raise HttpError('%s; no request was sent' % redactor(secret_list)(str(error))) from None
            if not sel:
                raise HttpError('HTTP run graph holds no request; no request was sent')
            if agent and any(FORBIDDEN_VARS.search(source) for source, _ in graph.docs.values()):
                raise HttpError('{{$dotenv}} / {{$processEnv}} are not available to a session')
        dynamic_output = False
        scripts_before_output = False
        for request in sel:
            request['runtime_inputs'] = dynamic_output
            scripts_before_output |= bool(request['pre'] or request['scripts'] or request['handler'])
            for directive in request['redirects']:
                directive.update(owner=request.get('owner', rel), file_values=request.get('file_values', file_values))
                fixed = all(m.group(1) in ('$projectRoot', '$historyFolder') for m in VAR_USE_RX.finditer(directive['path']))
                directive['static'] = fixed or not scripts_before_output and not VAR_USE_RX.search(substitute(
                    directive['path'], dict(values, **directive['file_values'])))
                dynamic_output |= not directive['static']
        runtime_refs, runtime_scripts = [], []
        refs = self._refs(ctx, rel, dict(parsed, requests=sel), agent, values, runtime_refs, runtime_scripts)
        if (runtime_refs or runtime_scripts) and not native_compatible:
            raise HttpError("runtime file inputs require the supported native CLI; no request was sent")
        for item in runtime_refs + runtime_scripts:
            item['position'] = next(i for i, request in enumerate(sel) if request['index'] == item['request'])
        script_refs = {x for r in sel for x in r['scripts']}
        body_refs = {x for r in sel for x in r['files'] if x not in r['scripts']}
        # Keep relative module paths intact in a private project mirror. Every dependency is
        # read before approval, included in its digest/card and checked against isolation.
        entries = [refs[ref] for ref in script_refs if ref in refs]
        source_lines = text.split('\n')
        module_sources, module_parsed = [], None
        try:
            for request in sel:
                owner = request.get('owner', rel)
                declarations = graph.docs[owner][1]['common'] if graph else parsed['common']
                for block in declarations:
                    module_sources.append((owner, block['code']))
                for span in (request['pre'], request['handler']):
                    if span:
                        source = '\n'.join(source_lines[span[0]:span[1] + 1])
                        source = source[source.index('{%') + 2:source.rfind('%}')]
                        module_sources.append((owner, source))
            if native_compatible and (entries or any(re.search(r'\b(?:import|export)\b', source) for _, source in module_sources)):
                def initial_module_read(path, limit):
                    try:
                        return common.safe_read(root, path, limit, single_link=True)
                    except FileNotFoundError:
                        if any(request['redirects'] for request in sel):
                            return None  # The native loader resolves and approves an exported dependency at run time.
                        raise
                modules, module_parsed = httpdeps.native_graph(binary, java_env(), os.path.join(HTTP_DIR, 'tmp'), root,
                    entries, [(owner, source) for owner, source in module_sources if re.search(r'\b(?:import|export)\b', source)],
                    ctx['hidden'] if agent else lambda _: False, int(self._http_policy().get('timeout') or 180), read=initial_module_read)
            else:
                for owner, source in module_sources:
                    entries.extend(httpdeps.module_path(owner, ref) for ref in httpdeps.module_dependencies(source))
                modules = httpdeps.module_graph(root, entries, ctx['hidden'] if agent else lambda _: False)
        except ValueError as error:
            raise HttpError('%s; no request was sent' % redactor(secret_list)(str(error))) from None
        for path in modules:
            alias = 'JS module: ' + path
            n = 0
            while alias in refs:
                n += 1
                alias = 'JS module (%d): %s' % (n, path)
            refs[alias] = path
            script_refs.add(alias)
        if graph:
            for path in graph.docs:
                if path != rel:
                    refs['HTTP import: ' + path] = path
        save_rel = ""
        if save_to:
            if index is None or len(sel) != 1:
                raise HttpError("save_to needs index: the response of ONE request goes to the file")
            if agent and cls == "prod" and not include_body:
                raise HttpError("saving a production response needs include_body (the user approves handing it over)")
            save_rel = self._save_target(ctx, save_to, agent)
        native_save = bool(save_rel and (graph or native_compatible or
                           any(r['pre'] or r['handler'] or r['scripts'] for r in sel)))
        legacy_save = bool(save_rel and not native_save)
        if native_save and not binary:
            raise HttpError('save_to with scripts requires the official ijhttp installation; no request was sent')
        if native_save and index is not None and not graph and not sel[0]['pre'] and not [
                ref for ref in sel[0]['scripts'] if ref != sel[0]['handler_file']]:
            # The CLI can transmit an unresolved header literally. Preserve the useful
            # login hint, and do not save an authentication error as the requested file.
            request = sel[0]
            setters = global_setters(text, parsed)
            missing = {m.group(1) for value in [request['url'], request['body']] +
                       [value for _, value in request['headers']]
                       for m in VAR_USE_RX.finditer(substitute(value, values)) if m.group(1) in setters}
            if missing:
                raise HttpError('; '.join('{{%s}} is set by request #%d %r' %
                                (name, setters[name]['index'], setters[name]['name']) for name in sorted(missing)) +
                                ': run it with index first (its globals are stored); no request was sent')
        directives = [d for r in sel for d in r["redirects"]] if not save_rel else []
        if directives and agent and cls == "prod" and not include_body:
            raise HttpError("saving a production response needs include_body (the user approves handing it over)")
        for d in directives:
            current = dict(values, **d['file_values'])
            if not VAR_USE_RX.search(substitute(d["path"], current)):
                self._redirect_target(ctx, d['owner'], d["path"], current, agent, d["overwrite"], secret_list)
        tdocs = self._trusted_env_docs(ctx, env_dir, public, private)
        _, sslconf = env_meta(*(tdocs + [{}, {}])[:2], env)   # a file a session writes must not turn TLS checks off
        vt = self._http_envcfg(key, env_dir, env).get("verify_tls")
        if vt is not None:   # Piper's own setting (the GUI) wins over the env file and the class policy
            sslconf.update(verifyHostCertificate=bool(vt), forced=True)
        private_ssl = (private.get(env) or {}).get(SSL_KEY) or {}
        has_certificate = isinstance(private_ssl, dict) and bool(private_ssl.get('clientCertificate') or private_ssl.get('clientCertificateKey'))
        if has_certificate and (not native_compatible or not any(doc is private for doc in tdocs)):
            raise HttpError('client certificates require the supported CLI and a private environment owned by the user; no request was sent')
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
        if native_compatible:
            java_configuration = java_env()
            java = os.path.realpath(os.path.join(java_configuration['JAVA_HOME'], 'bin', 'java') if java_configuration.get('JAVA_HOME')
                                    else shutil.which('java', path=java_configuration.get('PATH')) or '/nonexistent/bin/java')
            # localhost in an agent's container is a different trust context.
            # Preserve its login across files, without importing host credentials
            # or allowing its Set-Cookie to poison subsequent host/GUI runs.
            cookie_key = state_key + '\0sandbox\0' + ctx['session'].id if sbx else state_key
            try:
                jar = httpcookies.Jar(os.path.join(HTTP_DIR, 'cookies'), cookie_key, httpcookies.Suffixes(java), secret_list)
            except (OSError, ValueError):
                raise HttpError('HTTP cookie state could not be read safely; no request was sent') from None
        changed, warns, foreign = (False, [], [])
        auth = None
        try:
            configs = httpauth.configurations(public, private, env, values, substitute)
            if re.search(r'\$auth\b', text) or configs and (SCRIPT_RX.search(text) or script_refs):
                if not native_compatible or legacy_save:
                    raise ValueError('managed authentication requires the supported native CLI')
                auth = httpauth.Tokens(configs, secret_list)
        except ValueError as error:
            raise HttpError('%s; no request was sent' % redactor(secret_list)(str(error))) from None
        if agent:
            auth_requests = [dict(name='Managed authentication', url=url) for url in auth.endpoints] if auth else []
            changed, warns, foreign = self._agent_checks(ctx, text, sel + auth_requests, public, private, env, env_dir, refs, values)
            if sbx:
                foreign = [h for h in foreign if h not in LOCAL_HOSTS]
        if jar and jar.entries:
            warns.append('uses stored cookies from this project and environment; scripts can access authenticated responses')
        if auth:
            warns += ['managed authentication uses %s (token/device POSTs do not follow redirects)' % redactor(secret_list)(url)
                      for url in auth.endpoints]
        scrub = redactor(secret_list)
        brief = [{"name": r["name"], "method": r["method"], "url": scrub(substitute(r["url"], values))} for r in sel]
        # the snapshot - the text read above and its referenced files, copied BEFORE the card:
        # what runs is what was shown, whatever the sandbox writes meanwhile
        now = time.time()   # names sort by time: the newest run is the last name
        rid = time.strftime("%Y%m%d-%H%M%S-", time.localtime(now)) + "%06d" % int(now % 1 * 1e6) + _secrets.token_hex(2)
        work = os.path.join(HTTP_DIR, "tmp", rid)
        os.makedirs(HTTP_DIR, mode=0o700, exist_ok=True)
        os.makedirs(work, mode=0o700)
        inputs = code = None
        try:
            mapping = {}
            if jar is not None:
                write_json(os.path.join(work, 'cookie-broker.json'), {})
            script_maps = {}
            digest = hashlib.sha256(text.encode())
            if auth:
                digest.update(b'\0managed-auth\0' + auth.digest.encode())
                write_json(os.path.join(work, 'auth-broker.json'), {})
            if jar and jar.entries:
                # A prior grant for a public script does not cover newly available login state.
                digest.update(b'\0stored-cookie-scope\0' + state_key.encode())
            certificate = httpcerts.snapshot(root, env_dir, private_ssl, work) if has_certificate else None
            if certificate:
                for fingerprint in certificate['fingerprints']:
                    digest.update(b'\0client-certificate\0' + fingerprint.encode())
                secret_list.extend(certificate['secrets'])
                scrub = redactor(secret_list)
                warns.append('uses the configured client certificate (SHA-256 %s); scripts have access to its private run snapshot' %
                             ', '.join(certificate['fingerprints']))
            if graph:
                for path, (source, _) in sorted(graph.docs.items()):
                    digest.update(b'\0HTTP source\0' + path.encode() + b'\0' + source.encode())
            scripts = {}   # referenced script files: shown on the card with the .http file
            generated, outputs, reserved = [], {}, set()
            n = 0
            for request in sel:
                generated.append({ref: outputs[refs[ref]] for ref in request["files"]
                                  if ref not in script_refs and ref in refs and refs[ref] in outputs})
                for d in request["redirects"] if directives else []:
                    if d['static']:
                        target = self._redirect_target(ctx, d['owner'], d["path"], dict(values, **d['file_values']),
                                                       agent, d["overwrite"], secret_list)
                        target = self._redirect_name(root, target, d["overwrite"], reserved)
                        outputs[target] = os.path.join(work, "staged", "response-%d" % n)
                        reserved.add(target)
                    n += 1
            for n, (ref, p) in enumerate(sorted(refs.items())):
                uses = [i for i, r in enumerate(sel) if ref in r["files"]]
                if uses and all(ref in generated[i] for i in uses):
                    # No old snapshot: each later consumer uses the most recent producer's stage.
                    continue
                try:
                    data = (graph.docs[p][0].encode() if graph and p in graph.docs else
                            modules[p] if p in modules else common.safe_read(root, p, MAX_SAVE + 1))
                    if p not in modules and len(data) > MAX_SAVE:
                        raise HttpError('%s exceeds the %d byte input limit; no request was sent' % (ref, MAX_SAVE))
                except (OSError, ValueError) as e:
                    raise HttpError("cannot read %s: %s" % (ref, redactor(secret_list)(str(e)))) from None
                dst = (os.path.join(work, 'project', p) if ref in script_refs else
                       os.path.join(work, "ref%d-%s" % (n, os.path.basename(p))))
                os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(data)
                mapping[ref] = dst
                if ref in script_refs:
                    script_maps[dst] = [(p, i) for i, _ in enumerate(data.decode('utf-8', 'replace').split('\n'))]
                digest.update(b"\0" + ref.encode() + b"\0" + data)
                if agent and ref in body_refs and FORBIDDEN_VARS.search(data.decode("utf-8", "replace")):
                    raise HttpError("%s: {{$dotenv}} / {{$processEnv}} are not available to a session" % ref)
                if ref in script_refs or ref.startswith('HTTP import: '):
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
                         "digest": digest.hexdigest(), "scripts": {scrub(name):scrub(body) for name, body in scripts.items()}, "runtime_files": bool(runtime_refs),
                         "auth": [dict(name=scrub(name), grant=scrub(str(config.get('Grant Type') or config.get('Type') or '')),
                                       endpoint=scrub(config.get('Auth URL') if config.get('Grant Type') == 'Authorization Code' else config.get('Device Auth URL') if config.get('Grant Type') == 'Device Authorization' else config.get('Token URL') or '')) for name, config in auth.configs.items()] if auth else [],
                         "digest": digest.hexdigest(), "scripts": {k: scrub(v) for k, v in scripts.items()},
                         "evidence": facts, "_evidence_job": job, "request_scope": request_scope,
                         # a script (or a script file) can read the hidden values and return them
                         # transformed in a test name - the scrubbing cannot see that
                         "secret_scripts": bool((pub_hidden or private_hidden or certificate or auth or
                                                 jar and jar.entries and not sbx) and (SCRIPT_RX.search(text) or scripts))})
            def produced_input(relative, descriptor, deadline):
                def target(directive, path, iteration):
                    scoped, path_secrets = {}, secret_list
                    if VAR_USE_RX.search(path):
                        state = (live.finished(directive['request'], iteration, deadline) if descriptor.get('code') else
                                 live.before(directive['request'], iteration, descriptor['index'], descriptor.get('iteration'), deadline))
                        scoped = dict(environment_values, **flatten(state['globals']))
                        scoped.update(directive.get('file_values', {}))
                        scoped.update(flatten(state['variables']))
                        path_secrets = secret_list + secret_values(state['globals'], {}) + secret_values(state['variables'], {})
                    return self._redirect_target(ctx, directive.get('owner', rel), path, scoped, agent,
                                                 directive['overwrite'], path_secrets)
                return httpbroker.Generated(root, work, redirects, target, self._redirect_name, MAX_SAVE)(relative, descriptor, deadline)
            if runtime_refs:
                def approve_input(info, deadline):
                    callback = ctx.get('http_runtime_approve')
                    if agent and callback is None:
                        raise HttpError('runtime input needs a concrete per-file approval')
                    if callback:
                        detail = httpbroker.preview(info['data'], scrub)
                        if info.get('generated'):
                            detail['preview_note'] = 'from an earlier response; not published yet · ' + detail['preview_note']
                        callback({k:v for k,v in info.items() if k != 'data'} | detail | {'cls': cls}, deadline)
                inputs = httpbroker.Inputs(root, work, ctx['hidden'] if agent else lambda _: False,
                    approve=approve_input, forbidden=FORBIDDEN_VARS if agent else None, max_bytes=MAX_SAVE, generated=produced_input)
                for item in runtime_refs:
                    reference = substitute(item['reference'], {'$projectRoot': os.path.realpath(root),
                        '$historyFolder': os.path.join(root, '.idea', 'httpRequests')})
                    request = sel[item['position']]
                    origin = dict(file=item['owner'], index=request.get('root_index', request['index']),
                                  method=request['method'], target=scrub(request['url']))
                    generated[item['position']][item['reference']] = inputs.add(item['owner'], reference, origin, item['position'])
            rows = ([(ln, i) for i, ln in enumerate(text.split("\n"))] if index is None or graph else
                    single_rows(text, parsed, index))
            runtime_requests = parse('\n'.join(line for line, _ in rows))['requests']
            for item in runtime_scripts:
                request = runtime_requests[item['position']]
                for i in range(request['start'], request['end']):
                    match = (HANDLER_FILE_RX if item['phase'] == 'response' else REF_RX).match(rows[i][0].strip())
                    if match and match[1] == item['reference']:
                        rows[i] = (('> ' if item['phase'] == 'response' else '< ') + '{% ' + item['marker'] + ' %}', rows[i][1])
            owners = [request.get('owner', rel) for request in sel]
            blocks = [graph.docs[owner][1]['common'] if graph else parsed['common'] for owner in owners]
            rows = common_rows(rows, parse('\n'.join(line for line, _ in rows))['requests'], blocks, mapping, work, owners, script_maps, module_parsed)
            rows, restore_ref, restore_code = restore_globals_rows(rows, gl)
            if restore_ref:
                with open(mapping[restore_ref], 'r+', encoding='utf-8') as pre_file:
                    body = pre_file.read()
                    pre_file.seek(0)
                    pre_file.write(restore_code + '\n' + body)
                    pre_file.truncate()
                script_maps[mapping[restore_ref]] = [None] + script_maps[mapping[restore_ref]]
            rows = cookie_jar_rows(rows)
            if native_save:
                # save_to selects one response. Its native handlers/tests still run;
                # explicit save_to replaces its authored redirect in this private copy.
                rows = [(line, origin) for line, origin in rows if not SAVE_RX.match(line.strip())]
                rows += [('', None), ('>>! ' + save_rel, None)]
            redirect_vars = {m.group(1) for d in directives for m in VAR_USE_RX.finditer(d["path"])
                             if not m.group(1).startswith("$")}
            names = sorted(set(global_names(text + "\n" + "\n".join(scripts.values()))) | set(previous_globals) | redirect_vars)
            capture_runtime = True  # native identities also distinguish repeated names and iterations
            rows = instrument_rows(rows, names, variables=sorted(redirect_vars), force=capture_runtime)
            if capture_runtime:
                for ref in {r["handler_file"] for r in parse("\n".join(line for line, _ in rows))["requests"] if r["handler_file"]}:
                    # One native handler per response: append to its private snapshot, not a second handler.
                    with open(mapping[ref], "r+", encoding="utf-8") as f:
                        body = f.read()
                        f.seek(0)
                        f.write(capture_response_log() + '\n' + body + "\n" + capture_globals(names, sorted(redirect_vars)) + "\n")
                        f.truncate()
                    script_maps[mapping[ref]] = [None] + script_maps[mapping[ref]] + [None, None]
            runtime_marker = '__piper_runtime_' + _secrets.token_hex(12) + '_'
            live = httpruntime.Live(runtime_marker)
            if native_compatible:
                def approve_code(info, deadline):
                    self._http_approve_runtime_code(ctx, info, env, cls, rid, agent, scrub, deadline)
                code = httpdeps.Code(binary, java_env(), root, work, modules, ctx['hidden'] if agent else lambda _:False,
                                     approve_code, FORBIDDEN_VARS if agent else None, generated=produced_input, source_maps=script_maps)
                def code_context(position, phase, reference):
                    request = sel[position]
                    owner = request.get('owner', rel)
                    site = next((origin[0] for origin in script_maps.get(reference, []) if origin), owner)
                    dynamic = next((item for item in runtime_scripts if item['position'] == position and item['phase'] == phase), None)
                    return code.add(dict(index=position, phase=phase, trigger='script_path' if dynamic else 'computed_import', site=site,
                        **(dict(script_reference=substitute(dynamic['reference'], {'$projectRoot': os.path.realpath(root),
                            '$historyFolder': os.path.join(root, '.idea', 'httpRequests')}), root_marker=dynamic['marker'], owner=dynamic['owner']) if dynamic else {}),
                        origin=dict(file=owner, index=request.get('root_index', request['index']),
                                    method=request['method'], target=scrub(request['url']))))
            if capture_runtime:
                rows = runtime_rows(rows, runtime_marker, names, sorted(redirect_vars), mapping, work, script_maps,
                                    barriers=bool(inputs), native_modules=native_compatible,
                                    code_context=code_context if code else None)
            rows, redirects = redirect_rows(rows, work)
            if code:
                code.configure(set(script_maps) | {os.path.join(work, 'runtime', name) for name in os.listdir(os.path.join(work, 'runtime'))
                    if re.fullmatch(r'\d+-(?:pre|response)\.js', name)})
            for staged, original in zip(redirects, directives):
                staged.update(owner=original['owner'], file_values=original['file_values'])
            if redirects:
                write_json(os.path.join(work, 'export-targets.json'), {item['staged']: substitute(item['path'], {
                    '$projectRoot': os.path.realpath(root), '$historyFolder': os.path.join(root, '.idea', 'httpRequests')})
                    for item in redirects})
            pins = {}
            if sbx or native_save:
                rows = no_redirect_rows(rows, sel)
            if sbx:
                rows, pins = pin_rows(rows, text, sel, values)
            linemap = [o for _, o in rows]
            run_text = rewrite_refs("\n".join(t for t, _ in rows), mapping, generated)
            hf = os.path.join(work, 'project', rel)
            os.makedirs(os.path.dirname(hf), mode=0o700, exist_ok=True)
            with open(hf, "w", encoding="utf-8") as f:
                f.write(run_text)
            if graph:
                graph.write(work, list(zip(run_text.split('\n'), linemap)))
            pol = self._http_policy()
            started = time.time()
            raw, saved = b"", None
            relays, moved = [], {}   # the sandbox route: its relays, {relay port: the port the file names}
            def identity_stopped(reason):
                # This branch is before tunnels, issuer requests and native spawn.
                # Keep an explicit result for this attempt instead of leaving the
                # previous successful run on screen after a declined passphrase.
                requests = [dict(name=r['name'], method=r['method'], url=substitute(r['url'], values),
                                 status=None, status_text='', time_ms=None, ok=False, error=reason,
                                 tests=[], headers=[], req_headers=[], body='', body_size=0,
                                 root_index=r.get('root_index', r['index']),
                                 **(dict(source_file=r['owner'], source_line=r['source_line']) if graph else {})) for r in sel]
                rec = dict(id=rid, project=key, file=rel, env=env, cls=cls, env_dir=env_dir, sha=source_sha,
                           index=index, by=ctx['by'], at=started, ms=int((time.time() - started) * 1000),
                           rc=1, ok=False, requests=requests, summary='%d requests, none sent' % len(requests),
                           output='', changed=changed, share=cls != 'prod' or include_body)
                if any(r['no_log'] for r in sel):
                    rec['no_log'] = True
                self._http_store(rec, redactor(secret_list))
                return rec
            try:
                t0 = time.time()
                run_deadline = time.monotonic() + min(int(pol.get('timeout') or 180), AGENT_RUN_MAX if agent else float('inf'))
                if certificate and certificate['needs_passphrase']:
                    remaining = run_deadline - time.monotonic() - 5
                    if remaining <= 0:
                        return identity_stopped('client certificate passphrase not asked: run deadline reached; no request was sent')
                    entry = certificate['entry']
                    payload = dict(by='agent' if agent else 'gui', project=ctx['key'], file=rel, run=rid,
                                   env=env, cls=cls, entry={**entry, 'file':scrub(entry['file'])})
                    summary = ('⚠ Request #1 on %s needs the passphrase of its client certificate\n%s\n'
                               '%d bytes · sha256 %s\nSSLConfiguration · environment %s · %s · this run only\n'
                               'Kept in memory for this run; never stored or sent to the session. Piper does not read keychains for it.'
                               % (cls, scrub(entry['file']), entry['bytes'], entry['sha256'], env or '-', rel))
                    passphrase = self.ask_http_secret(ctx['session'] if agent else None, ctx['key'], summary, payload, remaining)
                    if passphrase is None:
                        return identity_stopped('client certificate passphrase not given; no request was sent')
                    try:
                        secret_list.extend(httpcerts.unlock(work, passphrase, timeout=max(.1, min(15, run_deadline - time.monotonic()))))
                    except ValueError as error:
                        return identity_stopped(str(error))
                    finally:
                        passphrase = None
                # Run exactly the env documents checked above, including $shared normalization;
                # never reopen mutable source env files after the approval card.
                env_path = os.path.join(work, "env") if env else None
                if env_path:
                    os.makedirs(env_path, mode=0o700)
                    for name, doc in ((ENV_FILE, public), (PRIVATE_FILE, private)):
                        if env in doc:
                            write_json(os.path.join(env_path, name), {env: doc[env]})
                run_values = values
                ports = {}   # every localhost:<port> of the run -> Piper's port
                if tun:   # Piper's own tunnel: every localhost:<declared port> of the run points at Piper's port
                    ports[tun["port"]] = self._http_tunnel_open(tun, ctx, cls)
                for port, host in (sbx.get("ports") or {}).items():
                    relays.append(SandboxRelay(self._sandbox_python(ctx["session"]) + ["-c", RELAY_SCRIPT, host, str(port)], native=native_compatible))
                    ports[port] = relays[-1].port
                    moved[relays[-1].port] = port
                def rw(v: str) -> str:
                    return ports_rewrite(v, ports)
                if ports:
                    env_path = os.path.join(work, "env")
                    os.makedirs(env_path, mode=0o700, exist_ok=True)
                    for n, d in ((ENV_FILE, public), (PRIVATE_FILE, private)):
                        if d:
                            write_json(os.path.join(env_path, n), rewrite_env(d, env, ports))
                    with open(hf, "w", encoding="utf-8") as f:
                        f.write(rw(run_text))
                    if graph:
                        graph.write(work, list(zip(run_text.split('\n'), linemap)), rw)
                    run_values = {k: rw(v) for k, v in values.items()}
                # the CLI ignores IDEA's SSLConfiguration (2026.1, verified): Piper turns it into --insecure
                insecure = cls in (pol.get("insecure") or []) or sslconf.get("verifyHostCertificate") is False
                if sslconf.get("forced"):
                    insecure = not sslconf["verifyHostCertificate"]
                # A tunnel changes only its own loopback endpoint. Its certificate
                # exception must not follow OAuth calls or native HTTPS redirects.
                tunnel_tls = [ports[tun['port']]] if tun and not insecure and not (
                    sslconf.get('forced') and sslconf['verifyHostCertificate']) else []
                if tunnel_tls and native_compatible:
                    write_json(os.path.join(work, 'tunnel-tls.json'), {'ports':tunnel_tls})
                def endpoint_insecure(url):
                    target = urllib.parse.urlsplit(url)
                    return insecure or target.hostname in ('localhost', '127.0.0.1', '::1') and target.port in tunnel_tls
                if auth and auth.endpoints:
                    auth.route = rw
                    auth.context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
                    tunnel_context = ssl._create_unverified_context() if tunnel_tls else auth.context
                    if certificate:
                        httpcerts.configure_context(auth.context, work)
                        if tunnel_context is not auth.context:
                            httpcerts.configure_context(tunnel_context, work)
                    auth.context_for = lambda url:tunnel_context if endpoint_insecure(url) else auth.context
                limit = int(run_deadline - time.monotonic())
                if limit <= 0:
                    raise HttpError('HTTP run deadline reached before execution; no request was sent')
                if legacy_save:
                    one = dict(sel[0], url=rw(pins[sel[0]["line"]])) if pins else sel[0]
                    row, raw = fetch_deadline(one, run_values, mapping,
                                              endpoint_insecure(substitute(one['url'], run_values)), limit)
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
                    network_failures = {}
                    capabilities = {}
                    from contextlib import nullcontext
                    destinations = run_targets([dict(r, url=rw(pins.get(r['line'], r['url']))) for r in sel], run_values)
                    if jar is not None:
                        jar.allowed = set(destinations)
                    def runtime_callback(message, deadline):
                        if auth is not None and message.get('kind') == 'auth':
                            return auth(message, deadline)
                        if code is not None and message.get('kind') in ('code_load', 'script_load', 'code_abort'):
                            return code(message, deadline)
                        if jar is not None and message.get('kind') in ('cookie_select', 'cookie_save'):
                            return jar(message, deadline)
                        if inputs is not None:
                            return inputs(message, deadline)
                        raise ValueError('unknown HTTP runtime message')
                    if auth:
                        def signin(name, flow, deadline):
                            payload = dict(by='agent' if agent else 'gui', project=key, file=scrub(rel), run=rid, env=scrub(env),
                                           backend=scrub(name), cls=cls, deadline=time.time() + max(0, flow.deadline - time.monotonic()),
                                           **{field:scrub(value) if isinstance(value, str) else value for field, value in flow.public.items()})
                            summary = ('⚠ Sign in yourself: this HTTP run on %s uses the token you get for %s; the session sees the responses, never the token\n'
                                       '%s · %s\n%s\nEnvironment %s · %s · this run only\n'
                                       'Deny or the run deadline stops authentication; earlier requests may have run; no request is replayed'
                                       % (cls, scrub(name), scrub(name), flow.grant, scrub(flow.public['provider']), scrub(env), scrub(rel)))
                            return self.ask_http_signin(ctx['session'] if agent else None, key, summary, payload, flow)
                        auth.signin = signin
                    with httpbroker.Broker(work, runtime_callback, limit) if inputs or jar is not None or code or auth else nullcontext():
                        try:
                            rc, console, xml = run_ijhttp(binary, work, hf, env, env_path, {}, insecure, limit,
                                                    connect_timeout_ms=pol['connect_timeout_ms'],
                                                    socket_timeout_ms=pol['socket_timeout_ms'],
                                                    targets=destinations,
                                                    network_failures=network_failures, capabilities=capabilities,
                                                    on_stdout=live.feed if inputs or code else None)
                        finally:
                            live.close()  # Wake a file callback before joining the broker after a native error.
                    try:
                        console, runtime_events = httpruntime.extract(console, runtime_marker)
                    except ValueError as error:
                        raise HttpError(str(error)) from None
                    junit_rows = parse_junit(xml)
                    reqs = merge(parse_console(console), junit_rows)
                    # A native client.exit has a valid Response report but no synthetic
                    # capture testcase. Ordered one-to-one reports retain an exact identity.
                    if len(reqs) == len(sel):
                        for i, response in enumerate(reqs):
                            if response.get('request_index') is None:
                                response['request_index'] = i
                    if rc in (87, 90, 91) and capabilities.get('binary_safe') and ({87:inputs, 90:code, 91:auth}[rc]):
                        # The confined JVM stops immediately on an unapproved runtime
                        # dependency. Its JUnit writer cannot finish: preserve the HTTP
                        # responses and identities actually emitted, label tests unknown.
                        failed = ((inputs.failure if rc == 87 else code.failure) or {}) if rc != 91 else {}
                        reqs = runtime_stopped(reqs, runtime_events, sel, failed.get('index'), run_values)
                        if rc == 90:
                            for response in reqs:
                                if response.get('request_index') == failed.get('index'):
                                    response.update(ok=False, error=failed.get('reason', 'runtime code refused') + '; earlier requests may have run; not retried')
                        if rc == 91:
                            for response in reqs:
                                response.update(ok=False, error='managed authentication failed; earlier requests may have run; not retried')
                    if rc and capabilities.get('binary_safe') and rc not in (87, 90, 91):
                        reqs = missing_results(reqs, runtime_events, sel, run_values)
                    httpruntime.apply(reqs, runtime_events)
                    for response in reqs:
                        if not response.get('status') and response.get('error'):
                            try:
                                destination = run_targets([response], {})[0]
                                reason = network_failures.get(destination)
                                if reason:
                                    response['error'] += ' - network broker: ' + reason
                            except HttpError:
                                pass
                    if sbx and not native_compatible:
                        for response in reqs:
                            unrelay_binary(response)
                    for response in reqs:
                        secret_list += secret_values(response.get('globals') or {}, {})
                        secret_list += secret_values(response.get('variables') or {}, {})
                    if native_save:
                        if len(reqs) != 1 or len(junit_rows) != 1:
                            raise HttpError('native save_to requires one complete response report; requests may already have run; not retried')
                        row = reqs[0]
                        if rc != 0:
                            row.update(ok=False, error=row['error'] or 'native run did not complete; response not saved')
                        if row['status'] is not None and 300 <= row['status'] < 400:
                            row.update(ok=False, error='HTTP %d - redirects are not followed when saving a response' % row['status'])
                        elif row['status'] is not None and not 200 <= row['status'] < 300:
                            row.update(ok=False, error=row['error'] or 'HTTP %d - response not saved' % row['status'])
                        if row['ok'] and rc == 0:
                            if row.get('binary') and not capabilities.get('binary_safe'):
                                row.update(ok=False, error='native binary response export is not byte-safe in this CLI; not saved')
                            else:
                                raw = common.safe_read(work, os.path.relpath(redirects[0]['staged'], work), MAX_SAVE + 1,
                                                       single_link=True)
                                if len(raw) > MAX_SAVE:
                                    row.update(ok=False, error='response exceeds %d MiB; not saved' % (MAX_SAVE >> 20))
                    elif directives and not complete_report(reqs, junit_rows, len(sel)):
                        for response in reqs:
                            response.update(ok=False, error=(response['error'] + ' - ' if response['error'] else '') +
                                            'response exports require a complete successful unambiguous native report; not published')
                    else:
                        self._publish_redirects(ctx, rel, redirects, reqs, environment_values, secret_list,
                                                agent, file_values, gl, binary_safe=capabilities.get('binary_safe', False),
                                                native_targets=capabilities.get('binary_safe', False))
            finally:
                for x in relays:
                    x.close()
            if save_rel and row["ok"]:
                try:
                    saved = self._save_response(ctx, save_rel, row, raw, secret_list + cookie_values([row]), agent)
                except (OSError, ValueError) as error:
                    row.update(ok=False, error='response not saved: %s; request already sent; not retried' % error)
            if save_rel:
                rc = 0 if row["ok"] else 1
        finally:
            if inputs:
                inputs.close()
            if code:
                code.close()
            shutil.rmtree(work, ignore_errors=True)
        reqs = [row] if save_rel else reqs
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
        for r in reqs:
            secret_list += secret_values(r.get('globals') or {}, {})
        log_scrub = redactor(secret_list + cookie_values(reqs))
        def source_message(message):
            message = relink_scripts(message, script_maps)
            return graph.relink(message) if graph else relink(message, hf, rel, linemap)
        for r in reqs:   # the run's copy -> the file the user knows, with its line numbers
            if r.get('script_log'):
                r['script_log'] = log_scrub(r['script_log'], cut=r.get('script_log_cut', False))
                r['script_log_limit'] = MAX_SCRIPT_LOG
            if sbx and not save_rel and not native_compatible:   # a binary body the legacy relay passed as text/plain
                unrelay_binary(r)
            # ...and the ports it names, not the relay's
            r["url"], r["error"] = ports_rewrite(r["url"], moved), ports_rewrite(r["error"], moved)
            r["error"] = hint_error(source_message(r['error']), r["url"], sbx.get("label", ""))
            for t in r["tests"]:
                t["message"] = source_message(t['message'])
        console = source_message(console)
        newg = dict(gl)
        captured = False
        for r in reqs:
            captured |= r.pop('captured', False)
            iteration = r.pop('iteration', None)
            identity = r.get('request_index')
            if graph and type(identity) is int and 0 <= identity < len(sel):
                descriptor = sel[identity]
                r.update(root_index=descriptor['root_index'], source_file=descriptor['owner'],
                         source_line=descriptor['source_line'])
            elif not graph:
                # Preserve the selected original request's index, rather than its
                # position in a single-request report or a possibly repeated name.
                r['root_index'] = index if index is not None else identity
            if type(iteration) is int:
                r['iteration'] = iteration
            for name in r.pop('unset', []):
                newg.pop(name, None)
            newg.update(r.pop("globals", {}) or {})
            r.pop('variables', None)   # runtime metadata is private; it may hold secrets
            r.pop('request_index', None)
            r["body_size"] = len(raw) if save_rel else r.get("body_size", len(r["body"]))
            if saved:
                r.update(saved)
        if captured or newg or gl:
            with _STORE_LOCK:   # re-read: another run may have stored meanwhile
                gstore = read_json(GLOBALS_FILE, {})
                gstore[state_key] = newg
                if legacy_allowed:
                    gstore.pop(legacy_state_key, None)
                write_json(GLOBALS_FILE, gstore)
        tail = agent_tail = ""
        if not reqs or rc:   # also preserve a native pre-script error after earlier successful responses
            def diagnostic_tail(value):
                return '\n'.join(ln for ln in value.split('\n') if ln.strip() and not ln.startswith('\tat ')
                                 and 'JAVA_TOOL_OPTIONS' not in ln)[-2000:]
            tail = diagnostic_tail(console)
            agent_tail = diagnostic_tail(log_scrub(console))
        if tail:
            tail = hint_error(tail, "")
        if agent_tail:
            agent_tail = hint_error(agent_tail, '')
        failed = sum(1 for r in reqs if not r["ok"])
        rec = {"id": rid, "project": key, "file": rel, "env": env, "cls": cls, "env_dir": env_dir,
               'sha': source_sha,
               "index": index, "by": ctx["by"], "at": started, "ms": int((time.time() - started) * 1000),
               "rc": rc, "ok": rc == 0 and bool(reqs) and not failed, "requests": reqs,
               "summary": "%d request%s, %d failed" % (len(reqs), "" if len(reqs) == 1 else "s", failed),
               "output": tail, "_agent_output":agent_tail, "changed": changed,
               "share": cls != "prod" or include_body}
        # A CLI report/console can contain several requests and their shared handler output.
        # If one opts out of history, retain none of the batch on disk; the immediate result
        # still reaches its caller. Global state and explicitly requested exports are separate.
        if any(request['no_log'] for request in sel):
            rec['no_log'] = True
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
        # the scrubbed copy for agents is computed now, while the secrets are known
        rec["agent"] = self._agent_view(dict(rec, output=rec.pop('_agent_output', rec.get('output', ''))), scrub)
        for row in rec['requests']:
            if len(row['body']) > MAX_BODY_GUI:
                row['body'] = row['body'][:MAX_BODY_GUI]
                row['body_cut'] = True
        if rec.get('no_log'):
            return
        os.makedirs(RUNS_DIR, mode=0o700, exist_ok=True)
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
            item = {"name": scrub(r["name"]), "method": scrub(r["method"]), "url": scrub(r["url"]), "status": r["status"],
                    "time_ms": r["time_ms"], "ok": r["ok"], "error": scrub(r["error"]),
                    "tests": [{"name": scrub(t["name"]), "ok": t["ok"], "message": scrub(t["message"])} for t in r["tests"]],
                    "headers": [[k, scrub(v)] for k, v in mask_headers(r["headers"])
                                if k.lower() in ("content-type", "location", "content-length", "content-disposition")],
                    "body_size": r.get("body_size", len(r["body"]))}
            for name in ('root_index', 'source_file', 'source_line', 'iteration', 'script_exited'):
                if name in r:
                    item[name] = scrub(r[name]) if isinstance(r[name], str) else r[name]
            for k in ("saved", "saved_bytes", "saved_note"):
                if r.get(k) and not r.get("saved_hidden"):
                    item[k] = scrub(r[k]) if isinstance(r[k], str) else r[k]
            if r.get('script_log'):
                item.update(script_log=scrub(r['script_log'])[:limit],
                            script_log_cut=bool(r.get('script_log_cut') or len(r['script_log']) > limit),
                            script_log_limit=min(MAX_SCRIPT_LOG, limit))
            ctype = next((v for k, v in r["headers"] if k.lower() == "content-type"), "")
            raw = r["body"]
            more = len(raw) - limit
            # Mask the complete response before applying either view's limits.
            body = scrub(raw)
            if r.get("saved") and not r.get("saved_hidden"):   # a file hidden from Claude is never named to it
                body = body[:limit] + ("\n… (%d more characters - the whole response is in %s)" % (more, item["saved"])
                                       if more > 0 else "")
            elif r.get("binary") or body and ctype and not is_text(ctype) and ("\ufffd" in body[:4096] or "\0" in body[:4096]):
                clen = next((v for k, v in r["headers"] if k.lower() == "content-length"), "") or str(item["body_size"])
                body = "(binary %s, %s bytes - run this request with index + save_to to get it as a file)" % (
                    scrub(ctype.split(";")[0]) or "response", scrub(clen))
            elif more > 0:
                body = body[:limit] + "\n… (%d more characters - index + save_to writes the whole response to a file)" % more
            item["body"] = body
            out.append(item)
        output = re.sub(r'(?m)^([ \t]*)([A-Za-z0-9!#$%&\x27*+.^_`|~-]+):[ \t]*(.*)$',
            lambda match:match[1] + match[2] + ': ' + mask_headers([[match[2], match[3]]])[0][1], rec.get('output') or '')
        metadata = {name:scrub(rec[name]) for name in ('file', 'env', 'summary') if isinstance(rec.get(name), str)}
        return {"requests": out, "output": scrub(output), **metadata}

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
        ctx = self._http_gui_ctx(key)
        result = self.http_envs(ctx, rel)
        source = self._http_read(ctx['root'], rel)
        parsed = parse(source)
        runs = [r for r in parsed['requests'] if r['method'].lower() == 'run']
        if runs:
            public, private = self._load_env(ctx['root'], result['env_dir'])
            previews = {}
            for env in [''] + [e['name'] for e in result['envs']]:
                values = dict(flatten(public.get(env) or {}), **flatten(private.get(env) or {})) if env else {}
                previews[env] = {}
                for request in runs:
                    try:
                        graph = httpgraph.Graph(ctx['root'], rel, source, request['index'], values,
                                                lambda _:False, parse, substitute)
                        _, flat = graph.flatten()
                        previews[env][str(request['index'])] = dict(sha=graph.stamps()[str(request['index'])],
                            requests=[dict(method=r['method'], url=r['url'], index=request['index'], name=r['name'])
                                      for r in flat['requests']])
                    except (OSError, ValueError):
                        previews[env][str(request['index'])] = dict(error='HTTP imports cannot be previewed safely; check the run declaration and dependencies')
            result['previews'] = previews
        return result

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

    @quietwindow.managed("http")
    def http_gui_run(self, key: str, rel: str, env: str, index: int | None, sha: str = "", save_to: str = "",
                     dependency_shas: dict | None = None) -> dict:
        """sha: of the text the user saw - a file a session rewrote meanwhile does not run (409).
        save_to: request `index`'s whole response into that project file (a binary one: a PDF)."""
        rec = self._execute(self._http_gui_ctx(key), self._norm(rel), str(env or ""), index, agent=False,
                            expect_sha=str(sha or ""), save_to=str(save_to or ""), dependency_shas=dependency_shas)
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
            strict = bool(self.policy.get("security_strict"))
            privileged = bool(self.policy.get("security_scoped")) and bool(info["secret_scripts"] or info["tunnel"] or info["sandbox_secrets"])
            if strict and foreign:
                raise HttpError("Strict refuses HTTP hosts outside the configured environment")
            if not strict and not privileged and not foreign and (not needs or cls != "prod" and ok_key in self._http_session_ok):
                return
            hosts = ", ".join(foreign)
            writes = [r["method"] for r in info["requests"] if r["method"].upper() not in ("GET", "HEAD", "OPTIONS")]
            # what matters first (the queue shows the first lines): the requests, then the details
            lines = ["HTTP requests on %s - environment %r, project %s" % (cls.upper(), env, ctx["key"])]
            if writes:
                lines.append("⚠ %d of them change data (%s)" % (len(writes), ", ".join(sorted(set(writes)))))
            lines += ["%d. %s %s" % (n + 1, r["method"], r["url"]) for n, r in enumerate(info["requests"])]
            if info.get("runtime_files"):
                lines.append("Runtime input files are approved individually after their paths are known; earlier requests may already have run.")
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
            for backend in info.get('auth') or []:
                lines.append('Auth backend (%s): %s · Piper acquires a token from %s only when first needed; credentials and tokens are never sent to the session' %
                             (backend['grant'], backend['name'], backend['endpoint'] or 'Mock (no issuer request)'))
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
                          "content_sha": info["digest"], "_evidence_job": info["_evidence_job"],
                          **({'auth':info['auth']} if info.get('auth') else {}),
                          "_force_approval": privileged or strict},
                          timeout=CARD_S, always="" if strict or privileged else always, result=res)
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

        def approve_runtime_input(info, deadline):
            remaining = deadline - time.monotonic() - 5
            if remaining <= 0:
                raise HttpError('HTTP runtime input approval deadline; requests may already have run; not retried')
            origin = info['origin']
            summary = ("⚠ Allow sends this file in request #%d to %s · earlier requests of this run may have run\n"
                       "File %s for %s (%s %s)\n%d bytes · sha256 %s\n"
                       "Picked by the pre-request script at run time · environment %r · this file, this run only\n"
                       "Waiting counts against the run deadline. Deny or no answer stops this input; nothing is replayed.\n\n%s" %
                       (origin['index'] + 1, 'PRODUCTION' if info['cls'] == 'prod' else info['cls'], info['path'],
                        origin['file'], origin['method'], origin['target'], info['size'], info['sha'], env,
                        info['preview'] if info['preview'] is not None else '(binary input; no text preview)'))
            payload = dict(file=info['path'], bytes=info['size'], sha256=info['sha'], origin=origin,
                           env=env, cls=info['cls'], danger=info['cls'] == 'prod',
                           deadline=time.time() + remaining, preview=info['preview'], preview_note=info['preview_note'],
                           binary=info['binary'])
            ok = self.ask('http_runtime_file', s, summary, payload, timeout=remaining)
            if not ok:
                raise HttpError('HTTP runtime input was declined or timed out; earlier requests may have run; not retried')
        ctx['http_runtime_approve'] = approve_runtime_input

        inline = args.get("text")
        if inline is not None and (not isinstance(inline, str) or not inline.strip()):
            raise HttpError("text must be the .http content")
        rec = self._execute(ctx, rel, env, index, agent=True, approve=approve, include_body=include,
                            save_to=save_to or "", text=inline)
        return self._agent_result(rec)

    def _agent_record(self, rid: str) -> dict:
        r = read_json(os.path.join(RUNS_DIR, rid + ".json"), None)
        if not r:
            raise HttpError("unknown run %s" % rid)
        return self._agent_result(r)

    def _agent_result(self, r: dict) -> dict:
        """Apply production sharing to a stored or deliberately non-persistent result."""
        view = r.get("agent") or {"requests": []}
        reqs = view["requests"]
        if not r.get("share"):
            reqs = [{k: v for k, v in x.items() if k not in ('body', 'script_log', 'script_log_cut', 'script_log_limit')}
                    for x in reqs]
        out = {"run": r["id"], "file": view.get('file', r['file']), "env": view.get('env', r['env']), "class": r["cls"], "by": "user (GUI)"
               if r["by"] == "gui" else "session", "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["at"])),
               "ok": r["ok"], "summary": view.get('summary', r['summary']), "requests": reqs}
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
