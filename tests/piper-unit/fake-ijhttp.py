#!/usr/bin/env python3
"""A stand-in for JetBrains' ijhttp CLI (test_httpclient): logs argv and the .http file it got
to $FAKE_IJHTTP_LOG (one JSON line per run), prints $FAKE_IJHTTP's console, writes its JUnit
report into the -r directory and exits with its rc."""
import json
import os
import sys

argv = sys.argv[1:]
spec = json.load(open(os.environ["FAKE_IJHTTP"]))
http_file = argv[-1]
with open(os.environ["FAKE_IJHTTP_LOG"], "a") as f:
    d = os.path.dirname(http_file)
    envs = {flag: open(argv[i + 1]).read() for i, flag in enumerate(argv[:-1]) if flag in ("-v", "-p")}
    f.write(json.dumps({"argv": argv, "text": open(http_file).read(), "envs": envs,
                        "refs": {n: open(os.path.join(d, n)).read() for n in os.listdir(d) if n.startswith("ref")}}) + "\n")
if "-r" in argv:
    rep = argv[argv.index("-r") + 1]
    os.makedirs(rep, exist_ok=True)
    with open(os.path.join(rep, "report.xml"), "w") as f:
        f.write(spec.get("xml", ""))
sys.stdout.write(spec.get("console", ""))
sys.exit(spec.get("rc", 0))
