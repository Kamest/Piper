#!/usr/bin/env python3
"""desktop-latest.py - the newest desktop release as the Pages manifest (desktop/latest.json).

Reads GitHub's release list (`gh api --paginate --slurp repos/OWNER/REPO/releases`, a list of pages or one
list) on stdin and prints {"tag_name", "html_url"} of the newest published, non-prerelease `desktop-vX.Y.Z`
release, or exits 1 when there is none. The desktop app reads this file from the Pages CDN instead of the
rate-limited anonymous GitHub API and still validates the tag and page itself.
"""
from __future__ import annotations

import json
import re
import sys

TAG = re.compile(r"desktop-v(\d+)\.(\d+)\.(\d+)")
PAGE = "https://github.com/Kamest/Piper/releases/tag/"


def latest(releases: list) -> dict | None:
    best, key = None, None
    for r in releases:
        if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
            continue
        tag = r.get("tag_name")
        m = TAG.fullmatch(tag) if isinstance(tag, str) else None
        if not m or r.get("html_url") != PAGE + tag:
            continue
        version = tuple(int(x) for x in m.groups())
        if key is None or version > key:
            best, key = {"tag_name": tag, "html_url": r["html_url"]}, version
    return best


def main() -> int:
    data = json.load(sys.stdin)
    if not isinstance(data, list):
        print("desktop-latest: not a release list", file=sys.stderr)
        return 1
    flat = [r for page in data for r in page] if data and all(isinstance(p, list) for p in data) else data
    found = latest(flat)
    if not found:
        print("desktop-latest: no published desktop release", file=sys.stderr)
        return 1
    print(json.dumps(found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
