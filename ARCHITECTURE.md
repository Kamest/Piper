# Architecture

## Design

Piper has a local Python daemon (`piper/piperd.py`), a separately running PTY host
(`piper/ptyhost.py`), a TypeScript web client (`piper/web/src`, compiled to the committed
`piper/web/dist`) and per-session agent processes. A Hole session runs in its own container sandbox;
a Direct session runs on this machine with the agent's native permissions and sandbox. Generated
instructions follow the session's runtime; container-only permissions never apply to Direct sessions.
The backend uses only the Python standard library; running Piper does not need Node.js.

The PTY host owns live terminals independently of daemon and browser lifetimes: piperd can be
restarted (`task piper:restart`) while sessions keep running. Stopping ptyhost ends every session.

Runtime state lives in `PIPER_HOME` (default `~/.piper`). The checkout is self-contained:
runtime paths resolve within it or through explicit user configuration.

The session manager derives activity from lifecycle hooks and explicit question tools. A bounded
native-conversation index can reconcile a missing completion hook for the same process and turn;
completion alone never confirms terminal readiness or authorizes automatic input. State decisions
are recorded in the existing audit journal without conversation text or tool arguments.

## Extension seam

Organization-specific behavior is loaded from extensions (`PIPER_EXTENSIONS`, a list of
directories with an `extension.py`; see `piper/extensions.py`). Hooks cover feature modules,
presets and integrations, defaults, settings fields, doctor rows, the dev stack provider,
protected and forbidden paths, allowed and reserved environment names, base rules, session
lifecycle, extra project and infrastructure roots, trusted git config and database links.
Without an extension Piper runs as is. Client features whose backend module is not registered are off.

The optional stack overview consumes a provider inventory of running roots and parent links. The core
validates and deduplicates that snapshot, dispatches explicit Stop requests and exposes bounded job
status; discovering containers and implementing their lifecycle remain the provider's responsibility.

Human-selected paths outside projects become external Files roots under the key `external:<absolute path>`.
Only the GUI file routes (`workbench.ext_*`) and `POST /api/fs/transfer` resolve that key, behind the full
GUI token. It is never a project for `projects.find` or `gui_project`, git, sessions or agent tools; it does not
register a project, share files with a session or widen agent filesystem grants. Every path, and every entry of
a folder operation, passes the same protection as `fs_target`: Piper's state, agent homes, extension
`protected` and `forbidden` hooks and `.git`, checked by name, realpath and identity with no-follow I/O.
A registered project reached through an external root can be read but not changed there. The transfer route
copies or moves files and folders between any two Files roots of one host: it checks everything first, writes
with O_EXCL, verifies by sha256 and never overwrites; replace and move go through Piper's trash, and a
failure is rolled back.

`scripts/piper.sh` is the CLI and service manager. A wrapper may set `PIPER_SERVICE_LABEL`,
`PIPER_LEGACY_LABEL`, `PIPER_EXTENSIONS`, `PIPER_SERVICE_ENV`, `PIPER_ENV_FILE` and `PIPER_CLI`
before running it; `restart` reinstalls a login service that was installed without the
wrapper's extension.

What changed per release, and how a release was verified, is in [CHANGELOG.md](CHANGELOG.md).
