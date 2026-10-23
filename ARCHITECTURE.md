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

What changed per release, and how a release was verified, is in [CHANGELOG.md](CHANGELOG.md).
