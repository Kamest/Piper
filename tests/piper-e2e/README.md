# Piper end-to-end tests

Everything here runs a real piperd + ptyhost from this checkout over stand-ins: fake-hole (`tests/piper-unit/fake-hole.sh`),
fake Docker, a fake ijhttp, a fake GitLab, a logging `open`. No test needs a real `hole`, a GitLab token or the network,
except `dbprod.py`'s driver install (PyPI) and `http_e2e.py` (JetBrains' ijhttp).

All of them in one go, skipping what the machine lacks (exit code non-zero on any failure):

    task piper:e2e

The local gate runs the suites sequentially so Chromium, databases and unit workers do not compete for
all the laptop's resources. Python runs at most two module processes by default (`PIPER_UNIT_JOBS` changes
that explicitly); Node also uses two workers. One GUI harness covers all three presets at both widths,
including the scenarios, instead of starting five independent daemons/browser profiles.
`piper:test` and `piper:e2e` share a per-user OS lock across worktrees: a second invocation waits. Individual
sandbox/module commands remain available for targeted debugging. Logs stream as the suites run and the
last line records their times. This trades some wall time for a usable machine, without reducing coverage.

While implementing use one module or `task piper:test` and a single `gui.py --<scenario>-only` instead of
the whole gate. Never run `piper:test` immediately before `piper:e2e`, which already includes it.

CI on a branch push runs only the build check and the unit job; run.py, dbprod and gui run nightly, on merge requests
and by hand.

Every script runs only inside the test sandbox: `python3 scripts/piper-sandbox.py -- python3
tests/piper-e2e/run.py ...` (`serve.py` too; a browser outside still reaches its loopback port). dbprod goes
through `--dind`, with the images copied in (`PIPER_SANDBOX_IMAGES`). `python3 scripts/piper-sandbox.py --selftest`
proves the sandbox holds. An extension brings its own suites (they import `run.py` the same way).

CI: `.gitlab-ci.yml` at the repository root (jobs `piper-*`).

Fuzz of `git_run` around hidden files with fresh seeds (~15 min): job `piper-fuzz`, only in a scheduled pipeline
(nightly) or started by hand. It prints the seeds (artifact `fuzz-seeds.txt`); a failure reruns locally with

    python3 scripts/piper-sandbox.py -- sh -c '(cd tests/piper-unit && N6_FUZZ_SEEDS=<seeds> N6_FUZZ_BUDGET=300 N6_FUZZ_N=2000 python3 -m unittest test_gitrun_exposed.ExposedPropertyTest)'

## The scripts

| Script | What it tests | Needs |
|---|---|---|
| `run.py` | T2: sessions, isolation, hidden files, approvals cards, git, GitLab (in-process fake), HTTP, conversations; ~25 sections, every check prints PASS/FAIL, `<work>/results.json` | python3 ≥ 3.10, git |
| `dbprod.py` | the Database module and the production check against real MySQL 8 + PostgreSQL (and Oracle / SQL Server when `DBPROD_ORACLE` / `DBPROD_MSSQL` name one); drivers installed through the product path | Docker containers from `dbseed/start.sh`, PyPI |
| `http_e2e.py` | `.http` runs with the real ijhttp: ssh tunnels, `save_to`, hints, masking (no piperd; the Manager in-process) | ijhttp, java, openssl; exit 77 = skipped |
| `serve.py` | not a test: the GUI harness. piperd + demo data, stays up until SIGTERM | as `run.py` |
| `gui.py` | the GUI regression gate: every view, dialog, the palette, the session menu and every Settings section under the presets basic / dev / all at 1024 and 1920 px, with the scans below on each screen; module visibility and real terminal clipboard checks (`--clipboard-only` for those alone) | `agent-browser` (+ its Chromium), Node 22+ for the clipboard permission fixture |
| `scans/*.js` | DOM scans, one line of doc at the top of each: `rlo.js` raw bidi / split ZWJ, `clip.js` text cut without an ellipsis, `reach.js` controls that cannot be clicked, `errors.js` JS / console errors and HTTP ≥ 400 (a page init script) | |
| `scans/allow.json` | accepted findings of `gui.py`: `{"scan", "match" (regex on the finding), "why", "width_max"?}`. Every entry names its reason; an open bug says so and goes when it is fixed | |

`serve.py`, `dbprod.py` and an extension's suites import `run.py` as a library (it parses its argv at import). `serve.py` uses
`setup_home`, `start_git_daemon`, `make_project`, `git`, `PLANTED`, `LEAK_EXCLUDES`, `mint`, `start_piperd`,
`ready_session`, `hook`, `stop`, `sess_dir`, `teardown` and the paths / state `A`, `WORK`, `PH`, `BIN`, `REAL`, `PIPER`,
`SOCK`, `ENV`, `PROC`, `TOK`, `PROJECTS`, `SECTIONS`: renaming one of them in `run.py` breaks the GUI harness.

`http_e2e.py` is not called `http.py`: the script directory is first on `sys.path`, and an `http.py` there would shadow
the stdlib `http` package for every script of this directory.

## Running one locally

Always outside any sandbox (sockets, PTYs, Docker):

    python3 tests/piper-e2e/run.py --port 17971 [--only L1,I12] [--work DIR --keep]
    python3 tests/piper-e2e/serve.py --port 17961          # prints: SERVE http://127.0.0.1:17961/#token=...
    python3 tests/piper-e2e/gui.py --port 17961            # starts its own serve.py; --url URL reuses a running one
    MYSQL_PORT=13361 PG_PORT=15461 sh tests/piper-e2e/dbseed/start.sh -ci
    DBSEED_SUFFIX=-ci DBPROD_MYSQL=127.0.0.1:13361:root:e2e-root-pw:shop DBPROD_PG=127.0.0.1:15461:postgres:e2e-pg-pw:e2e \
      python3 tests/piper-e2e/dbprod.py --port 17981
    docker rm -f piper-test-mysql-ci piper-test-pg-ci
    PIPER_IJHTTP=/path/to/ijhttp python3 tests/piper-e2e/http_e2e.py

`serve.py` for a manual GUI pass: open the printed URL in a browser (or `agent-browser open <url>`), stop it with
Ctrl-C or `kill <pid>` (the pid is in `<work>/serve.json`). It removes its temp work dir unless `--keep` / `--work`.
The deep exploratory GUI passes stay manual / agent-driven; `gui.py` is the regression gate after them.

## Ports

Each harness takes a base port `P` and also uses `P+1` (git daemon) and `P+7` (an http env host nothing listens on);
`serve.py` adds `P+2` (fake GitLab). `task piper:e2e` defaults: gui/serve 17961, run.py 17971, dbprod 17981, MySQL 13361,
PostgreSQL 15461.

## Environment

| Variable | Used by | Meaning |
|---|---|---|
| `PIPER_E2E_RUN_PORT`, `PIPER_E2E_DB_PORT`, `PIPER_E2E_GUI_PORT` | `task piper:e2e` | the base ports above |
| `PIPER_E2E_MYSQL_PORT`, `PIPER_E2E_PG_PORT` | `task piper:e2e` | host ports of the `piper-test-*-ci` containers |
| `DBPROD_MYSQL`, `DBPROD_PG`, `DBPROD_ORACLE`, `DBPROD_MSSQL` | `dbprod.py` | `host:port:user:password:database` of the servers |
| `DBSEED_SUFFIX` | `dbprod.py` | the suffix `dbseed/start.sh` got (DB-RECOVER `docker exec`s into those containers) |
| `MYSQL_PORT`, `PG_PORT`, `ORA_PORT`, `MSSQL_PORT`, `WITH` | `dbseed/start.sh` | ports; `WITH="oracle mssql"` adds those two |
| `SERVE_MYSQL`, `SERVE_PG` | `serve.py` | `host:port:user:password:database`: a dev connection of grp/demo in the Database view |
| `SERVE_DBVENV` | `serve.py` | a driver venv copied to `PIPER_HOME/dbvenv` (else the Database view offers the install) |
| `AGENT_BROWSER` | `gui.py`, `task piper:e2e` | the agent-browser binary (default: the one on `PATH`) |
| `AGENT_BROWSER_ARGS` | agent-browser | Chromium flags, e.g. `--no-sandbox` as root in CI |
| `PIPER_IJHTTP` | `http_e2e.py` | the ijhttp launcher (default: `ijhttp` on `PATH`) |

## Safety rules

- Never against the live piperd (`:17895`) or `~/.piper`: every harness has its own `PIPER_HOME`, `HOME` and port
  under a temp dir; the scripts refuse port 17895.
- Never a real `hole` with a test `HOME`: it removes the shared registry and the agent images. The harnesses only
  run fake-hole.
- Everything a harness starts is killed by PID at the end (piperd, ptyhost, git daemon, the stand-in ssh);
  never `pkill` by pattern - a test piperd has the same command line as the real one.
- Containers: `piper-test-*` names (`-ci` suffix from `task piper:e2e`), from local images only, removed afterwards.
- Only fakes: no real token or password; the database passwords are the throwaway ones of the test containers.

## Profiling GUI time

Add `--profile` to the sandboxed `gui.py` command to print `GUI_PROFILE`: command counts and
elapsed time by command kind, without payloads. The settle timing includes its browser calls;
do not add it to the command totals. Use the same presets and widths for before/after comparisons.
The default settle waits for rendered state; explicit timing/polling regression intervals remain.
