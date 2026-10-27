#!/usr/bin/env python3
"""A stand-in for JetBrains' ijhttp CLI (test_httpclient): logs argv and the .http file it got
to $FAKE_IJHTTP_LOG (one JSON line per run), prints $FAKE_IJHTTP's console, writes its JUnit
report into the -r directory and exits with its rc."""
import json
import os
import base64
import re
import sys

argv = sys.argv[1:]
spec = json.load(open(os.environ["FAKE_IJHTTP"]))
http_file = argv[-1]
with open(os.environ["FAKE_IJHTTP_LOG"], "a") as f:
    d = os.getcwd()
    refs = {os.path.relpath(os.path.join(parent, name), d): open(os.path.join(parent, name)).read()
            for parent, _, names in os.walk(d) for name in names
            if name.startswith('ref') or name.endswith('.js')}
    envs = {flag: open(argv[i + 1]).read() for i, flag in enumerate(argv[:-1]) if flag in ("-v", "-p")}
    tunnel_tls = json.load(open('tunnel-tls.json')) if os.path.isfile('tunnel-tls.json') else None
    f.write(json.dumps({"argv": argv, "text": open(http_file).read(), "envs": envs, 'tunnel_tls':tunnel_tls,
                        "refs": refs}) + "\n")
for target, body in zip(re.findall(r'^>>!?\s+(.+)$', open(http_file).read(), re.M), spec.get("saved_bodies", [])):
    target = re.sub(r"\{\{__piper_stage_[0-9a-f]+\}\}", "", target)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        f.write(base64.b64decode(body))
if "-r" in argv:
    rep = argv[argv.index("-r") + 1]
    os.makedirs(rep, exist_ok=True)
    with open(os.path.join(rep, "report.xml"), "w") as f:
        f.write(spec.get("xml", ""))
sys.stdout.write(spec.get("console", ""))
sys.exit(spec.get("rc", 0))
