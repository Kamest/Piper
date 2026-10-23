#!/usr/bin/env python3
"""tests/piper-e2e/cards.py - raise approval cards in a running serve.py harness (F17 browser checks).

Each argument is `<project>:<tool>[:<json args>]`; the request goes into that project's session mailbox as
piper-mcp.py writes it and is NOT waited for - the card stays until the GUI answers it (or it times out).
    python3 tests/piper-e2e/cards.py <work>/serve.json grp/demo:git_push 'grp/demo:piper_allow_domain:{"domain":"api.example.org"}'
Run it in the test sandbox like serve.py.
"""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
    ph = os.path.join(cfg["work"], "ph", "sessions")
    for spec in sys.argv[2:]:
        project, tool, *rest = spec.split(":", 2)
        sid = cfg["sessions"][project]
        args = json.loads(rest[0]) if rest else {}
        box = next(os.path.join(ph, sid, d) for d in ("claude", "piper") if os.path.isdir(os.path.join(ph, sid, d)))
        out = os.path.join(box, "piper-mailbox", "out")
        os.makedirs(out, exist_ok=True)
        rid = os.urandom(16).hex()
        tmp = os.path.join(out, ".%s.tmp" % rid)
        with open(tmp, "w") as f:
            json.dump({"id": rid, "tool": tool, "args": args}, f)
        os.rename(tmp, os.path.join(out, rid + ".json"))
        print("sent %s %s %s" % (project, tool, rid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
