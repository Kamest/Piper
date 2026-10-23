"""The Python unit suite, one process per test module, several at a time.

A single `unittest discover` ran ~15 min: every module shares one process, so threads and state left by
earlier modules slowed later ones (a timesheet test: 0.6 s alone, 9 s in the full run). Each module here
gets its own interpreter - and with it its own _env ROOT, HOME, ptyhost socket and PIPER_PORT.

Run it like the old command, inside scripts/piper-sandbox.py, from the same working directory:
  python3 tests/piper-unit/run_parallel.py [-s DIR] [-p 'test_*.py'] [-j JOBS]
The tail is unittest's own ("Ran N tests in S", "OK (skipped=K)" / "FAILED (...)"); a failing module's
whole output is printed. PIPER_UNIT_JOBS overrides the number of workers.
"""
import argparse
import concurrent.futures
import fnmatch
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# modules whose test classes run as separate processes: test_gitrun_exposed alone took 207 s in one
SPLIT_BY_CLASS = {"test_gitrun_exposed.py"}


def parse_result(text: str) -> dict:
    """unittest's summary of one module: tests run, failures, errors, skipped, ok."""
    ran = re.findall(r"^Ran (\d+) tests? in ", text, re.M)
    status = re.findall(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", text, re.M)
    counts = {"failures": 0, "errors": 0, "skipped": 0, "expected failures": 0, "unexpected successes": 0}
    ok = False
    if status:
        word, detail = status[-1]
        ok = word == "OK"
        for part in filter(None, (p.strip() for p in (detail or "").split(","))):
            k, _, v = part.partition("=")
            if k.strip() in counts and v.strip().isdigit():
                counts[k.strip()] = int(v)
    return {"ran": int(ran[-1]) if ran else 0, "ok": ok and bool(ran), **counts}


def summarize(results: list[dict], wall: float) -> tuple[str, bool]:
    """The tail of the whole run in unittest's words; a module without a summary (crashed) fails it."""
    total = {"ran": 0, "failures": 0, "errors": 0, "skipped": 0, "expected failures": 0, "unexpected successes": 0}
    ok = True
    for r in results:
        for k in total:
            total[k] += r[k]
        ok = ok and r["ok"] and r["rc"] == 0
    crashed = sum(1 for r in results if r["rc"] != 0 and not r["failures"] and not r["errors"])
    parts = ["%s=%d" % (k, total[k]) for k in ("failures", "errors") if total[k]]
    if crashed:
        parts.append("crashed modules=%d" % crashed)
    parts += ["%s=%d" % (k, total[k]) for k in ("skipped", "expected failures", "unexpected successes") if total[k]]
    tail = "Ran %d tests in %.3fs\n\n%s%s" % (total["ran"], wall, "OK" if ok else "FAILED", " (%s)" % ", ".join(parts) if parts else "")
    return tail, ok


def test_classes(path: str) -> list[str]:
    """The TestCase classes of a module that hold tests (named *Test), in file order."""
    with open(path) as f:
        return re.findall(r"^class (\w+Test)\(", f.read(), re.M)


def units(directory: str, names: list[str]) -> list[tuple[str, str]]:
    """(label, target): a whole module, or module.Class for SPLIT_BY_CLASS."""
    out = []
    for n in names:
        classes = test_classes(os.path.join(directory, n)) if n in SPLIT_BY_CLASS else []
        out += [("%s:%s" % (n, c), "%s.%s" % (n[:-3], c)) for c in classes] or [(n, "")]
    return out


def run_module(directory: str, name: str, target: str = "") -> dict:
    t0 = time.time()
    if target:   # one class: by name, the module importable from the test directory (cwd stays as it is)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [directory, os.environ.get("PYTHONPATH")])))
        argv = [sys.executable, "-m", "unittest", target]
    else:
        env, argv = None, [sys.executable, "-m", "unittest", "discover", "-s", directory, "-p", name]
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    r = parse_result(p.stdout)
    r.update(name=name, rc=p.returncode, output=p.stdout, secs=time.time() - t0)
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-s", "--start", default=HERE)
    ap.add_argument("-p", "--pattern", default="test_*.py")
    ap.add_argument("-j", "--jobs", type=int, default=int(os.environ.get("PIPER_UNIT_JOBS") or 0))
    a = ap.parse_args(argv)
    names = sorted(n for n in os.listdir(a.start) if fnmatch.fnmatch(n, a.pattern) and n.endswith(".py"))
    # the largest files first (test_sessions, ~100 s): the longest module bounds the wall time
    names.sort(key=lambda n: -os.path.getsize(os.path.join(a.start, n)))
    if a.jobs < 0:
        ap.error("jobs must be positive (0 selects the default)")
    jobs = a.jobs or min(2, os.cpu_count() or 1)
    t0 = time.time()
    results = []
    with concurrent.futures.ThreadPoolExecutor(jobs) as ex:
        for f in concurrent.futures.as_completed([ex.submit(run_module, a.start, label, tg) for label, tg in units(a.start, names)]):
            r = f.result()
            results.append(r)
            print("%-52s %-6s %4d tests %6.1fs" % (r["name"], "ok" if r["ok"] and r["rc"] == 0 else "FAILED", r["ran"], r["secs"]), flush=True)
    for r in results:
        if not (r["ok"] and r["rc"] == 0):
            print("\n" + "=" * 70 + "\n%s (exit %d)\n" % (r["name"], r["rc"]) + "=" * 70 + "\n" + r["output"], flush=True)
    tail, ok = summarize(results, time.time() - t0)
    print("-" * 70 + "\n" + tail, flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
