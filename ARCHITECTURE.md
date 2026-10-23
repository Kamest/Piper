# Architecture

## Design

Piper has a local Python daemon (`piper/piperd.py`), a separately running PTY host
(`piper/ptyhost.py`), a TypeScript web client (`piper/web/src`, compiled to the committed
`piper/web/dist`) and per-session agent sandboxes started through hole. The backend uses only
the Python standard library; running Piper does not need Node.js.

The PTY host owns live terminals independently of daemon and browser lifetimes: piperd can be
restarted (`task piper:restart`) while sessions keep running. Stopping ptyhost ends every session.

Runtime state lives in `PIPER_HOME` (default `~/.piper`). The checkout is self-contained:
runtime paths resolve within it or through explicit user configuration.

## Extension seam

Organization-specific behavior is loaded from extensions (`PIPER_EXTENSIONS`, a list of
directories with an `extension.py`; see `piper/extensions.py`). Hooks cover feature modules,
presets and integrations, defaults, settings fields, doctor rows, the dev stack provider,
protected and forbidden paths, allowed and reserved environment names, base rules, session
lifecycle, extra project and infrastructure roots, trusted git config and database links.
Without an extension Piper runs as is. Client features whose backend module is not registered are off.

`scripts/piper.sh` is the CLI and service manager. A wrapper may set `PIPER_SERVICE_LABEL`,
`PIPER_LEGACY_LABEL`, `PIPER_EXTENSIONS`, `PIPER_SERVICE_ENV`, `PIPER_ENV_FILE` and `PIPER_CLI`
before running it; `restart` reinstalls a login service that was installed without the
wrapper's extension.

## 2026-10-23: first CI fixes

The test sandbox runs in its own work directory, so the workflow calls the suites by
`$GITHUB_WORKSPACE` paths. The runner image ships a global `tsc` of another version whose source
maps differ; `scripts/piper-build.sh` uses a global `tsc` only when it is `TSC_VERSION`, otherwise
npx with the pinned version (`tests/piper-unit/test_piper_build.py`). The unit job then caught
`svc_install` on systemd failing under `set -u` without `$USER`; it falls back to `id -un`.

## 2026-10-23: public examples and documentation

Examples use reserved domains and documentation IP addresses. Source comments describe the
implementation directly; design notes and release checks live in this repository. IDE state
is excluded from source control. Core imports are checked against the standard library and
local modules, with database drivers allowed only in the isolated worker. Push previews use
common mainline names without organization-specific branch conventions.

Verified: the public-content regression tests run through `scripts/piper-sandbox.py`,
`sh scripts/piper-build.sh --check`, and the HTTP environment dialog rendered by agent-browser
inside the OS test sandbox. The SSH placeholder shows a documentation address.

The relevant Python suites ran 352 tests (339 passed, 13 skipped); the common and test-sandbox
suites passed another 34 tests. The client suite passed 771 tests with 2 skips. Skipped
cases and the full end-to-end release gate still require verification on a suitable host.
