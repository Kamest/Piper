# Piper reference

A local GUI to run several interactive Claude Code or Codex sessions, each in its own hole sandbox
with its root in one project, plus a **Global agent** (internally `advisor`) session that plans the work
and starts the project sessions. Sessions keep running when the GUI closes or piperd
restarts. An agent must never see excluded files. Nothing starts Piper unless you do.

This is the complete reference. New here? Start with the [user guide](../docs/GUIDE.md). GitLab, GitHub,
Mattermost, IntelliJ IDEA, the morning production check, Time and remote access are in
[Integrations](INTEGRATIONS.md). See [Architecture](../ARCHITECTURE.md) for the component boundaries and
[Security](../SECURITY.md) for reporting vulnerabilities.

## Quiet windows for heavy work

**Quiet window** is enabled by default in **Settings → Features**, including on
existing installations. An enabled feature does not reserve the machine: normal
sessions keep working until someone submits a request. Agents may request a window
only when the user explicitly asks for quiet or exclusive machine time. A build,
benchmark or request to test thoroughly does not authorize an automatic reservation.

Open a local running session's **⋯ → Request a quiet window…**. First use explains
three steps; **How it works** opens the explanation again. Describe the work and
request it until your session releases it. There is no default duration limit, including for hours-long work. Builds, large test runs, imports and local model
runs can all use the same window.

Other Piper sessions checkpoint and safely interrupt their own heavy work, confirm that their background tasks
and subagents have ended, then wait. The Overview and full workspace show who is
finishing, waiting or owns the window. The owner's name and reason are visible from
the request form onward, including while waiting; **Open** goes directly to that session.
**Back to Quiet window** returns from the session. Your queued request shows both
the current holder and your requester, with a separate **Cancel my request** action.
The Overview summary also links to the owner. Ended sessions remain available for inspection;
a removed session retains its last known information without a broken link.
The participant rows also show whether the request was delivered, the latest reported
work and whether that report has become stale. **Open** inspects that participant.
A long task can finish safely while the owner keeps renewing: two minutes without
an update raises a warning, not a false grant or an automatic resume. The owner keeps the window until explicitly releasing it; a three-hour job uses the same flow. A request can be cancelled
while preparing, including recovery before it was ever granted; an already granted
window still requires checked release.
Start heavy work only after **Quiet · [owner] can start work**. Several consecutive tasks can use one
window; finishing one task does not release it. **Release window** asks you to confirm
that all work has ended before other sessions continue.

Agents use `quiet_request`, `quiet_status`, `quiet_ack`, `quiet_wait`, `quiet_renew`,
`quiet_work`, `quiet_release` and `quiet_cancel`. Agent requests require
`user_requested:true`, including through the legacy `piper_session` fallback;
this attests to an explicit user request, not merely a heavy workload. An owner must renew at least every
60 seconds. Run long native commands in the background so the agent can keep checking
and renewing while they run. Piper observes synchronous managed operations throughout
their execution. An omitted duration has no deadline. Old API clients may explicitly request a bounded 10/15/30-minute window; renewing that legacy request does not extend its deadline.
Other agents first report `quiet_ack` with `finished=false` and a short `detail`,
updating at least every 60 seconds while finishing existing work. Progress never
confirms quiet or renews the owner's supervision. Only after all work/children end
do they send `finished=true` and use bounded `quiet_wait` calls; they check the
current status before continuing because another request may be queued.

Sessions already running before this update do not acquire new MCP tools until
their next start. Piper includes compatible controls through their existing `piper_session` tool in each notice.
**Details → Activity** records delivery, progress, confirmation, work and release,
distinguishing agent reports, operator actions and Piper observations. Check-ins do
not fill the log. History starts with this update; older windows are not reconstructed.
In **Technical details**, **Confirm waiting…** is a fallback only for an unconfirmed
session without a recent update, after checking that all of its work has ended and
it will remain waiting. Already-confirmed sessions have no confirmation action.
**Still needed** manually confirms the owner's continuing need; use it at least
every 60 seconds when the owner cannot renew itself. It cannot revive a window whose supervision was lost. This is a fallback; normal check-ins are handled by the owner agent.


Codex requires approval of new or changed native hooks. Review the Piper hooks
with `/hooks` before relying on the native checkpoint; an untrusted hook is
skipped. Server-side admission checks and cooperative/operator confirmations
remain separate from native hook enforcement. Before confirming, native tools remain
available cooperatively to stop and check existing jobs. General native tools are sealed
before the confirmation is published; failed sealing prevents the confirmation.
Piper-managed new operations remain blocked throughout preparation.

New GUI starts can **Wait until the window ends** without losing the entered task
or start options. These explicit intents survive a controller restart. If a crash
leaves the result of a start uncertain, Piper shows it for checking and never blindly
replays it. A selected new worktree is also created only after release. A start
already being dispatched cannot be removed as if it were still waiting; an uncertain
record after restart requires confirmation after checking Overview. Removing that
record does not stop a session. Requests for windows are served in order.

A lost owner, expired check, legacy explicit time limit or controller restart shows **Needs help**;
other sessions remain held. Inspect the owner and its background work, then release
the window after checking. Continue notices are stored before release and retried across controller restarts.
Released rows distinguish **Asked to continue** (notice delivered) from **Resume pending**
(with the delivery blocker); neither claims the agent is already running. A changed runtime
is never sent an old resume notice.
The module cannot be switched off until outstanding
windows and queued starts are released or removed.

This is cooperative coordination on this computer's Piper controller. Piper gates
its managed operations and supplies native tool checkpoints to newly started Claude
and Codex sessions. Already running or unsupported native clients depend on their
cooperation. Existing shells, shared services and applications outside Piper may keep
running; this does not freeze OS processes or guarantee exclusive CPU/IO. SSH sessions
are not a distributed reservation of the laptop.

## Servers and remote agents

Open **Settings → Servers** to add or edit SSH connections. Local and remote sessions
share the normal queue. A server name appears beside its sessions and in Session info;
only the selected server becomes unavailable if its connection drops.

In **New session**, choose **Host**, then the project, Claude/Codex and **Hole** or
**Direct**. Changing the host keeps your task and agent, and resets the project and
worktree. With no servers configured, only the local Sandbox choice is shown.
**Resume conversation** has its own host and history list. SSH-only servers offer
new direct sessions; saved history, resume and fork need the service.

Session, project, terminal, HTTP run and DB result identities retain their host through
callbacks, caches and requests. A different selection or browser tab cannot redirect
an in-flight operation. Settings and the global Database view belong to this computer;
a session's Database pane belongs to its project on its own host. Remote state never
replaces the notebook's appearance or settings. An unavailable server retains its last
known sessions and terminal buffers; it may still be working independently.

Add a server with a name, an existing SSH alias (recommended) and an absolute projects
folder. Piper creates its internal identity automatically. The saved host, folder and
connection mode are fixed; add another connection to change them. Piper uses your SSH configuration and verified host keys. Log
in once in a terminal to establish a new host key; password/device login and model
credentials belong to the server account, and are never copied from the notebook.
The first check stores a private account identity in `~/.cache/piper-ssh/identity`;
Piper pins it locally and verifies it before subsequent operations, including terminals.
A changed target requires a verified new environment ID. SSH must support
noninteractive authentication, and the server needs Python 3.10+
(Codex with an existing config.toml needs Python 3.11+).

**Server service** is recommended for full controls. **Add and connect** saves and checks a connection. **Test again** detects the
agents, service and user manager. **Install service** installs the
Piper controller under `~/.local/share/piper-server`, state under `~/.piper-server`,
and `systemd --user piper-server.service`. It requires Linux/systemd, uses loopback
17896 and SSH transport, and does not use sudo or expose a public port. Claude/Codex
must already be installed on the server and available on the SSH PATH or in `~/.local/bin`. New server sessions default to direct mode.
To survive the last SSH logout, enable user lingering (`loginctl enable-linger`);
server policy may require an administrator. The check reports lingering separately
from a running service. Updating the controller keeps its PTY host and existing agents.
With lingering and the enabled user service, the controller also starts automatically
after a server reboot, before SSH login. A reboot ends agent processes; it does not
continue an interrupted model turn or replay its task automatically.

**SSH only** declines installation of the Piper application/service. Python, tmux
and the selected native agent must already exist. One-shot helpers discover projects
and manage detached tmux sessions; small session records live in `~/.cache/piper-ssh`.
Supported controls are start in an existing checkout, terminal, stop, rename and remove.
Login/approval prompts and detailed state stay in the terminal. Piper files/Git views,
structured MCP/approvals, resume/fork/handoff and worktree creation require the service.
Native background APIs and SDK/App Server adapters are future compatibility options;
tmux is the implemented service-free supervisor for both agents.

In **New session**, select Claude/Codex and **Direct** or **Hole**.
Direct execution keeps native login/history and adds per-session hooks and Piper MCP;
it does not rewrite personal agent configuration. Codex uses workspace-write/on-request.
Hole options apply only to Hole sessions. Existing sessions keep their original runtime.
Recent Codex versions may ask you to review session hooks. Review and approve them in
the terminal; Piper does not bypass that decision. The Composer waits while a login,
trust or hook review dialog is open.
Without Hole, launch the controller with `PIPER_DEFAULT_RUNTIME=direct` (for example
`PIPER_DEFAULT_RUNTIME=direct sh scripts/piper.sh serve`); its installation check then
checks native agents instead of Docker/Hole.

**Optional local HTTP / DB tools** maps one saved server project to one registered
local project. New direct and Hole server sessions get a separate `piper_local` MCP
server. Requests use the session mailbox; the server controller forwards them through
a loopback reverse SSH tunnel (17897). No additional Hole network permission is needed;
the scoped provider capability stays outside the container. Local credentials and existing approval
rules stay on the notebook. The grant cannot run arbitrary Piper/shell tools or select
another local project. Revoke disables it locally even when the server is offline.
One controller can own that reverse port on a server; collisions fail closed.
A five-second availability check precedes each operation, including with a silent
SSH tunnel. A failed check sends no operation. If the connection fails after sending
one, its outcome is uncertain and Piper does not retry it automatically.

Remote Hole requires Hole and an accessible Docker engine on the server; the Piper
service installer does not install them. Choose **Hole** in **New session**.
The sandbox, login/configuration and project are server-owned and survive notebook
disconnect. SSH-only/tmux supports direct execution; use the service for Hole.

After notebook disconnect, server files, native agent tools/login and server-owned
Piper HTTP/DB remain available where configured. Notebook-provided HTTP/DB and new
GUI decisions are unavailable until reconnect. A running session may wait for an
approval or expired login. Neither disconnect nor process exit proves task completion.
Piper does not automatically replay a lost mutation. A local tool call reserves its
request ID durably; an unknown outcome requires inspection before a new request.

## Optional mobile access through an encrypted relay

Answer agents and approve requests from your phone, without a VPN, port forwarding
or anything to host. Off until you start it:

```sh
task piper:mobile -- install   # once: the optional mobile runtime
task piper:mobile -- serve     # keep it running; Ctrl-C turns mobile access off
```

Then pair in **Settings → Mobile access → Pair phone** and scan the QR code. The
phone opens the hosted app at [piper.kamest.dev](https://piper.kamest.dev), which
reaches this computer through the open relay `relay.kamest.dev`. Both only pass
end-to-end encrypted traffic; your Piper's own pairing decides who gets in.
Prefer your own infrastructure? See [Self-hosting](#self-hosting-the-relay-and-the-phone-app) below.

### Pairing and notifications

Once the bridge is running, open **Settings → Mobile access → Pair phone**
in the local computer's Piper. Name the phone and choose **Show QR code**, then
scan with its camera and confirm **Pair with full access** on the phone. The code
works once for 60 seconds. **Cancel**, Escape and closing the dialog invalidate
it; an expired code can be replaced with **Generate new code**.

For the installed iPhone PWA, choose **Copy pairing link** on the computer and
paste it in the app's **Pairing link** field, then **Continue**. Safari and Home
Screen storage may be separate. Manage paired phones in the same Settings section;
**Remove… → Remove phone** also closes live connections. These controls require
the local desktop GUI; mobile/remote clients cannot administer pairing. No command
is needed for normal pairing or removal. Treat the QR/link as a password.

On the phone, **Settings → Mobile access → Notifications → Turn on notifications**
enables optional Web Push. On iPhone/iPad (iOS 16.4 or later), add Piper to the
Home Screen and open that installed app first; allow notifications when asked.
**Send test** checks sending, and **Turn off** unsubscribes this phone.
On the first paired open, a one-time strip offers **Turn on notifications** or
**Not now**. It does not open the OS permission dialog until you tap Turn on.
Safari on iPhone offers Home Screen steps first. Dismissal is remembered per
browser/PWA; Settings remains available later.
Notifications can arrive with Piper closed on the phone, but the paired computer
must remain online with Piper and its mobile bridge running. Focus, OS settings
and the push provider can delay or suppress display; “sent” is not a delivery receipt.

By default the bridge encrypts a generic alert. After enabling notifications,
**Show session names** in the same panel optionally includes the session name
for this phone; names can appear on its lock screen. Messages, Piper's bearer
and pairing keys are never included. The bridge sends directly to the browser's
Apple/Google/Mozilla push service. The provider still sees delivery
metadata; opening the alert loads details over the existing authenticated encrypted
connection. VAPID and subscription capabilities stay in private `mobile.json`.
Removing a phone stops subsequent sends; an already dispatched alert cannot be recalled.
No additional relay domain or push gateway is needed. Updating an existing bridge
requires installing the updated optional requirements. The static export includes
root `/service-worker.js`; preserve it and the supplied security headers when deploying.
The worker handles push/click only and does not cache application data or requests.

### Self-hosting the relay and the phone app

This is administrator setup, not the phone pairing flow.

The Safari/web client reaches this computer without a VPN or a public PC address.
Browser traffic always uses the relay. By default that is the open `relay.kamest.dev`
with the phone app from `piper.kamest.dev`; the relay admits any client, so relay
admission is not an access control: who may use your Piper is decided only by the
per-phone pairing below. To run both yourself you need **a trusted static HTTPS
client origin and an iroh relay with a valid HTTPS certificate**, reachable by both
devices. Use a standard self-hosted relay; authentication for managed Shared or
Dedicated Relays is not integrated. Give a public relay rate limits; a private one
can add a shared token (below). Requests are limited to 16 MiB, responses to
32 MiB and individual terminal frames to 4 MiB.

```sh
task piper:mobile -- install
task piper:mobile-export -- /absolute/empty/public-directory --relay https://relay.example
# Deploy that directory to your trusted HTTPS origin, preserving _headers or equivalent.
task piper:mobile -- serve --relay https://relay.example --name 'My computer'
# Open Settings → Mobile access to pair and manage phones.
```

For a private relay using iroh's `access.shared_token`, start the bridge with
`--relay https://relay.example --relay-token-file /absolute/private/relay.token`.
The token file must be owned by the current user and inaccessible to other users
(mode `0600`). Its contents grant relay use only; they are separate from Piper's
local bearer and device pairing grants. The capability travels in the private
pairing fragment and phone profile, never in the public static export. Configure
the relay proxy to redact the `token` query parameter from both access and error
logs; preferably move it into `Authorization` and strip it from the upstream URL.
Revoking a phone removes its Piper access. Rotate the shared relay token separately
if relay admission must also be withdrawn; rotation requires updating other clients.

Piper must already be running locally. `serve` stays in the foreground; stopping it
stops mobile access without restarting piperd or its sessions. The `link`, `devices` and `revoke` CLI subcommands remain administrator fallbacks.
The optional Python
runtime is separate from Piper's normal dependencies. The prebuilt WASM is included;
only transport development needs Rust (`task piper:mobile-build`). Export to the
origin root, not a subdirectory. Use one computer profile per client origin/browser.
Protect the pairing link like a password; do not paste it into shared logs or chats.

The phone explicitly confirms **full Piper access**, including terminals, files,
database operations and approvals. Pairing pins both endpoint keys, works once and
expires after 60 seconds. Removing a phone also closes live connections within one second;
an operation already sent to Piper may finish. Queued requests recheck their exact
grant immediately before dispatch; re-pairing cannot revive an old request.
Unpaired consent waits have a separate small connection budget, preserving paired
client capacity. Network/handshake floods still need relay-level controls.
Lost mutations are never retried by
the transport. The ordinary Piper bearer stays on this computer. Device keys and
grants are in private `PIPER_HOME/mobile.json`; phone keys are in browser storage.
Clearing that storage requires pairing again. Back up/protect these keys as credentials.

This does **not** promise absolute security. Iroh authenticates and encrypts transport;
the relay still sees metadata and can interrupt service. Your mobile HTTPS host can
replace JavaScript and compromise keys or decrypted data, so its deployment access
is part of the trust boundary. A compromised device, XSS or an unlocked stolen phone
also defeats this protection. Public n0 relays are for development/testing; choose
and operate suitable production infrastructure yourself. The native FFI binds its
UDP sockets to loopback and checks that at startup; its portmapper cannot currently
be disabled through the Python API and may still probe/request router mappings.

Safari on a real iPhone, iOS installation/storage separation and Wi-Fi/mobile-data
handover still require device validation before production use. Pairing in Safari
does not establish that an installed PWA shares the key. The implementation provides
the web transport and static client; it does not install a native iOS application.

## Quick start

1. `task piper:doctor` - what this machine still needs (hole ≥ 2.1.0, Docker, the Docker VM type, git, Python ≥ 3.10);
   every line that is not `ok` names its fix.
2. `task piper:install-service` - piperd starts at login (or `task piper:open` once).
3. `task piper:open` - the GUI. A new install shows the **setup card**: the same checks, and which parts of Piper
   you want to see (Basic / Developer / Everything; Settings → Features later).
4. **+ Session**, pick a project/runtime/agent and write the task. Sign in in the terminal if needed.

The sections below describe configuration, daily use and known limits.

## Try the demo

Run `piper demo` (or `sh scripts/piper.sh demo` from this checkout). It creates
`$PROJECT_PATH/piper-demo`, a tiny Python package with one deliberately failing test,
and starts an ordinary session in the running Piper using Claude Code. Use
`piper demo --agent codex` to choose Codex, or `piper demo --no-start` to prepare only.
If piperd is stopped, the command prints the next steps: `piper open`, then `piper demo`.
Keep the same `PROJECT_PATH`, `PIPER_HOME` and `PIPER_PORT` for both commands.

The agent fixes addition, runs `python3 -m unittest discover -s tests -v`, commits,
and pushes through Piper to a **local bare origin** under `$PIPER_HOME/demo-remotes/`,
outside the project roots. No remote service or credentials are needed for the push.
The task then requests optional access to `docs.python.org` with `piper_allow_domain`.
Fresh installs (the Recommended profile) commit on their own; the push and the network
permission change ask on **approval cards**. Allow or deny them in the GUI. Allowing the domain
affects future starts of this demo only; no download is needed. Custom Security settings may behave differently.

With a ready sandbox image and an authenticated agent, this is intended to reach the card
in about two minutes. First-time image creation and agent login can take longer.
Rerunning reuses the marked demo and keeps your files, fixes and commits. A foreign
`piper-demo` directory is refused. If you already allowed the domain, the agent reports
that existing decision instead of asking again. Other projects are left alone.

## Extensions

`piper extensions` (or `sh scripts/piper.sh extensions --json`) lists registered extensions and manifests
found up to two directories below the projects folder, its parent and this checkout's parent. Discovery
only reads `piper-extension.json`; it never runs the code. Use `piper extensions trust <path>` to consent,
`untrust <path>` to revoke. Trust is stored privately in `$PIPER_HOME/trusted-extensions.json`. An explicitly
trusted path is also remembered outside the scan roots. With exactly one trusted, API-compatible extension,
the CLI delegates to its wrapper with `PIPER_SRC` pointing here; the wrapper owns the environment. Multiple
trusted extensions leave the CLI running the core alone and doctor explains why. `PIPER_NO_EXTENSIONS=1`
disables delegation. Existing wrappers may still set `PIPER_EXTENSIONS` directly. Install prompts distinguish
registered extensions (`--yes` accepts) from scan results (a terminal answer or `--extension <path>` is required);
`--no-extensions` runs the installer with the core alone.

## First time

1. Prerequisites: python3, git and a native Claude/Codex CLI for direct execution;
   Hole ≥ 2.1.0 and Docker additionally for Hole sessions (node is not needed).
   `PROJECT_PATH` comes from your shell, from the file `PIPER_ENV_FILE` names, or it defaults to the
   directory that holds this checkout.
2. `task piper:test`: the unit tests, including a full start against a fake hole. Every test runs
   inside the test sandbox of `scripts/piper-sandbox.py` (Seatbelt on macOS, bwrap or a container on
   Linux), and a test started any other way refuses to run. Start it **outside** any
   Claude/Codex sandbox: Seatbelt cannot be nested, and those sandboxes deny PTYs and sockets.
3. Choose which `~/.claude` the sessions get (**Settings › Sessions › Sandbox**, or `claude_dir` in `~/.piper/policy.json`):
   - `"session"` (default): each session gets its own `~/.claude`, built from
     `~/.piper/claude-template`. Every start (and `task piper:template`) syncs your Claude
     configuration into it (CLAUDE.md, settings, status line and the files settings refer to,
     skills, commands, agents, hooks; never transcripts, history or plugins). An item you
     edited in the template itself is kept; a template from before 2026-10-05 is backed up to
     `claude-template.backup-<time>/` the first time. A `/login` in a session is saved to the
     template at its next prompt (or when another session starts) and serves the next starts.
   - `"host"`: every session mounts your real `~/.claude` read-write, like a manual
     `hole start` with the personal global: same login, status bar, plugins, history.
     Piper's own pieces (MCP config, hooks, mailbox, dropped images) live in
     `~/.piper/sessions/<id>/piper`, mounted at `~/.piper`, and are passed with
     `--mcp-config`/`--settings`/`--append-system-prompt`; nothing is written into your
     `~/.claude`. **K2 accepted:** a sandbox then reads the transcripts of all your
     sessions and can change hooks and settings that later run on the host. The GUI
     inspector says so on every such session.
   In Settings, `host` asks for a confirmation (K2). By hand, `{"claude_dir": "host"}` is enough; other policy keys keep their defaults.
4. `task piper:open`: starts piperd and opens the GUI with its token. Every git checkout under
   `$PROJECT_PATH` is offered as a project (key `client/repo`). `~/.piper/projects.json`
   adds GitLab paths or a profile to one, and `"globs": ["~/development/*"]` there offers every
   git checkout a pattern matches (key = last two path segments, `development/my-app`).
5. Optional: extensions (`PIPER_EXTENSIONS`, a list of directories with an `extension.py`, see
   `piper/extensions.py`) add what is specific to your environment - e.g. a dev stack provider for the
   **Stack** section, defaults like your GitLab server, doctor rows. GitLab, GitHub and the other
   services: [Integrations](INTEGRATIONS.md).
6. A project session also mounts the **libraries** of your `~/.hole/settings.json` (they
   move into a generated profile `project`; the Global agent mounts none). Each library needs
   its own `<library>/.hole/settings.json` with excludes, and so do files your excludes
   anchored at the root do not reach. A blocked start offers **Add excludes**, which
   writes exactly those paths (for a library also `.hole/` into its `.git/info/exclude`) and
   approves what it wrote itself; a settings file you had not approved yet still shows its diff.
   **Allow in this project** is for files the session needs: after confirmation they are accepted
   in that mount root only (also for later sessions). Other roots are still checked and protected
   mounts remain blocked. This lasts until hole applies excludes to libraries
   (agreed upstream).
7. Optional, so the GUI is always there without a command: `task piper:install-service`
   installs a login service (a launchd agent on macOS, a systemd `--user` unit on Linux).
   piperd then starts at login, and launchd/systemd restart it within ~10 s when it dies.
   `task piper:ensure`/`piper:stop`/`piper:restart` drive the service from then on, and
   `task piper:uninstall-service` removes it. Sessions survive all of these, because ptyhost
   runs in its own session. Only `task piper:stop -- all` ends them. The GUI on your other devices:
   [remote access](INTEGRATIONS.md#remote-access-over-your-tailnet).
8. First session (`claude_dir` `session`): answer `/login` in its terminal. The login is
   copied back to the template when the session ends. With `host` it is your own login. The first start builds the sandbox image, which takes
   minutes.

**Later pulls:** `task piper:restart` picks up a new piperd, and running sessions survive.
Only when `piper/ptyhost.py` changed ([CHANGELOG](../CHANGELOG.md) says so) is `task piper:stop --
all` + `task piper:open` needed, and that **ends every session**. Resume them with
Continue. Until that restart, features that need the newer ptyhost say so and the rest works as before.

## API access for tests

Select the environment keys a project's tests may use in the host's `~/.piper/projects.json`.
The values stay in a regular dotenv file of the registered checkout:

```json
{
  "projects": {
    "qa/playwright-tests": {
      "path": "/path/to/qa/playwright-tests",
      "sandbox_env": {
        "file": ".env.local",
        "keys": ["QA_API_URL", "QA_API_USER", "QA_API_PASSWORD"]
      }
    }
  }
}
```

`sandbox_env` is optional and applies to new project sessions, for both Claude and Codex.
Piper reads only the selected keys on the host and passes them as environment variables to that
project's sandbox. Tests in linked or agent-created worktrees inherit them too; no dotenv file
needs to be copied into a worktree. Other projects, sessions that merely mount this checkout as a
library, and the Global agent do not receive these values. The session's isolation check allows only its selected names; the global
secret-variable policy stays in place. Missing or empty keys and symlinked sources stop the start
with an error naming the key or file. Dotenv content is parsed literally, never sourced or expanded.
Generated settings and audit records contain names and references, never the values.

Keep the source path relative to the registered checkout. Process-control variables such as `HOME`,
`PATH`, `GIT_*` and `PIPER_*` cannot be selected. The API host must also be allowed by the project's
network configuration. After changing the code, run `task piper:restart`; running sessions survive,
but their sandbox must be started again to receive new environment values.

**Docker Desktop / Docker VMM (K18):** older Hole hid directories with nested host binds,
which libkrun could silently drop. Hole 2.4 uses a per-instance Docker volume instead,
with tmpfs fallback on engines without volume subpaths. File excludes still use `/dev/null`.
Piper recognizes these masks and checks their contents inside the container. Doctor's
mount test exercises host-bind masks for Hole 2.1–2.3 and the tmpfs fallback for 2.4+;
passing it alone does not verify volume subpaths.

## Use

`projects.json` in `$PIPER_HOME` is the project whitelist (the advisor names a key, never
a path). **Settings → Projects** edits it (keys, paths, GitLab projects, profile, glob patterns;
automatically found checkouts are listed with **Add**); by hand it looks like:

```json
{ "projects": { "shop": { "path": "/Users/me/www/acme/shop",
                          "gitlab": ["acme/shop"] } } }
```

| Task | What |
|---|---|
| `task piper:open [-- --print]` | ensure piperd, open (or print) the GUI URL |
| `piper` / `piper status` / `piper stop` … | the same from any directory; the first `task piper` links `~/.local/bin/piper` (when that dir is on PATH, as Claude Code's installer sets it); no command = `open` |
| `task piper:ensure` / `piper:restart` | background piperd; restart after an update (sessions survive) |
| `task piper:stop [-- all]` | stop piperd; `all` also stops ptyhost, which **ends every session** |
| `task piper:template` | sync the Claude config template from `~/.claude` now (every start does it too; never transcripts or history) |
| `task piper:build [-- --check]` | rebuild `web/dist` (committed) / verify it and the vendored xterm.js |
| `task piper:test` | unit tests (stdlib; PTY cases skip where PTYs are unavailable) + the GUI's node tests |
| `task piper:install-service` / `piper:uninstall-service` | login service: launchd agent `piperd` (`PIPER_SERVICE_LABEL`, `~/Library/LaunchAgents/`) or systemd `--user` unit, with this shell's `PATH`, `PROJECT_PATH`, `PIPER_*` baked in; re-run it after you change those |
| `task piper:status` | is piperd up, is the service installed |
| `piper attach [<id\|project\|.>]` / `task piper:attach -- …` | a session in **this** terminal (below) |

### A session in your own terminal (`piper attach`)

For those who live in Ghostty (or iTerm, Terminal): the session runs in your terminal tab
**and** stays in the GUI at the same time.

| Command | What |
|---|---|
| `piper attach` | pick a live session by number; inside a project checkout also `n` = a new session there |
| `piper attach <project>` | its session; none live = a new one (preflight, isolation check, audit as from the GUI). `.` = this directory's project; the last path part is enough (`shop2024`) |
| `piper attach <id>` | that session (a unique prefix of the ID is enough); an ended one offers Continue |
| `--new` · `--replay` · `--take` · `-l` | another session in the project · replay the recent output · take it over from another terminal · list |

- **Ctrl-]** detaches. Closing the tab detaches too. The session keeps running and stays in
  the GUI. Ctrl-C, Esc and the rest go to Claude.
- **While attached, your terminal sets the size.** The GUI no longer resizes that terminal:
  selecting the session opens its **Conversation** (read, reply); its Terminal tab shows the
  terminal shrunk to fit the pane. Where that would be unreadable (under 9 px) a card offers
  **Conversation · Use here · Just look** (Just look: smaller still, the rest pans with the wheel).
  **Use here** (also in the bar above the terminal) takes the session over: the terminal
  detaches and says so, the GUI sizes it again. Detaching in the terminal does the same.
- **One terminal per session:** a second `piper attach` says so, `--take` takes it over.
- What only the GUI shows reaches the terminal as a line: a confirmation card waiting for
  you (git push…; the tab rings), a notice. Decide the card in the GUI.
  **Allow for this session** remembers that action for this session, separately on DEV / TEST / PROD.
  It survives a Piper restart; a new session asks again. Production grants are red and require a click.
- When the session ends in the terminal (`/exit`), the client offers **Continue** there.
- **GUI → terminal:** **Open in terminal** (the session’s `⋯` menu, row context menu, ⌘K) opens a new window
  attached to the session in the terminal **Settings → Terminal** names: Ghostty (the default
  when installed), Warp (Piper writes `~/.warp/launch_configurations/piper-attach.yaml` and
  opens it) or Terminal (a `.command` file in `~/.piper/run`). After Ctrl-] you stay in a shell
  in the project (git next to it). **Linux:** `$TERMINAL`, `x-terminal-emulator`, then the
  desktop's terminal (GNOME / KDE / Xfce / kitty / Alacritty / WezTerm / foot / xterm). **WSL:**
  Windows Terminal (else a console via `cmd.exe`). With no terminal the GUI copies `piper attach <id>`.
- **Notifications:** with the GUI closed, piperd notifies on the host (macOS notification
  centre) when a session you used from a terminal needs you, and when one that is not
  attached any more waits for you. `PIPER_DESKTOP_NOTIFY=0` turns it off.
- Not in the terminal: the history before you attached (the GUI has it, or `--replay`) and
  pasting images (drop them in the GUI).

## How a start works

**Settings › Security** offers **Minimal** (16 guards/cards), **Recommended** (37) and **Strict**
(all 64 plus enforced sandbox and approval rules). New installations start with Recommended. On upgrade,
an exact match to an old Minimal, Recommended or Strict profile moves to its new version. Any deviation
remains **Custom** with its previous behavior. The old format stored only switches, so an identical
manually assembled set also counts as that old profile. Current-revision manual saves stay Custom,
even when their switches match a profile; choices already frozen by the previous migration also stay
Custom because their original intent is no longer distinguishable. A manual switch change returns to Custom.
Four production/outside-environment approvals always remain enabled. Open **Customize details** to
search or adjust individual switches. Start checks and sandbox contents apply on the next start; tool
limits, unlocks and approval choices apply at once. Strict requires a fresh, verified Hole session,
private agent homes and read-only libraries; it forbids production DB writes and broad approval grants.
Its effective suboptions appear read-only without overwriting the stored preferences. Hole's own
excludes, safe path validation and the prohibition on committing over-mounted files remain in effect.

1. **Preflight (L1, L3):** `guard.py` scans the project and every library it will mount
   for sensitive candidates (`policy.json`, defaults in `guard.DEFAULT_POLICY`) and
   simulates hole's exclusion. An uncovered candidate, a changed or forbidden
   `.hole/settings.json` key, or a mount that would expose `$PIPER_HOME`,
   `~/.claude`, `~/.hole`, Piper's own checkout or what an extension protects blocks the start. Changed settings
   can be approved in the GUI; everything else has to be fixed. `library_allow`
   (`{host path: [patterns]}`) accepts candidates in one mount root only; the blocked
   start's **Allow in this project** writes it for you (after a confirmation). `.git` is no
   candidate since git is open (2026-10-01; the scan does not walk into it either).
2. **hole-home (Hole 2.4+):** `holehome.py` generates `$PIPER_HOME/sessions/<id>/hole-settings.json` from
   your `~/.hole/settings.json`:
   - removes the includes of `~/.claude`, `~/.gemini`, `~/.sage` and `~/.gitconfig`,
   - turns every other directory include except `~/.m2` into a read-only library,
   - removes `GITLAB_TOKEN`, the GitLab host and port 2375,
   - makes libraries read-only (except those in `holehome.rw_libraries`, Settings → hole
     defaults; a session can then plant `.git/config` or `.git/hooks` there that git on the
     host runs); `.git` is no longer excluded since 2026-10-01 (git works in the sandbox;
     `force_exclude: [".git"]` in Settings hides it again; a linked worktree also gets its main
     repository's `.git` read-write, taken from the recorded repository root),
   - wraps host hooks so they run with your real `HOME`,
   - gives every session its own **Maven settings** (`m2settings.py`, Settings › Sessions › New sessions ->
     `maven_settings`, default `auto`): `<box>/piper/maven-settings.xml` and
     `maven-toolchains.xml`, included onto `~/.m2/settings.xml` / `~/.m2/toolchains.xml`,
     generated at every start from your `~/.m2/settings.xml` - the profiles that can be active
     (activeProfiles, activeByDefault, a jdk/os/property/file activation) or define repositories,
     the servers their repositories use (repository logins, a package registry's `Deploy-Token`),
     `JAVA_*_HOME` pointed at the image's JDKs (`dependencies`: `openjdk-<v>-jdk*`);
     properties-only profiles nothing activates, unused servers and proxies stay on the host.
     With no maven-cache include of your own, the host's `~/.m2/repository` is included too
     (as a plain hole's include would). Your own include onto `~/.m2/settings.xml`, `~/.m2` or
     the project's `.hole/settings.json` include onto it wins and nothing is generated for it.
     Each session has its own global file, so simultaneous starts cannot swap their Maven includes.
     Shared agent/trust preparation remains serialized until hole's first output (`_spawn_lock`).

   `HOLE_SETTINGS_FILE` selects that file explicitly; an inherited override is replaced.
   New sessions use your real `HOME`, so host hooks and paths inside the sandbox agree with
   a manual Hole run. Native starts use your normal Hole registry, trust and logs without
   overwriting your global settings or agent files.
   Each session pins its HOME in metadata. Existing sessions without that field keep
   `$PIPER_HOME/hole-home`, including on retry/resume; new and old sessions can run together.
   Custom agents are read from your original `~/.hole/agents`; Piper never filters their
   `allow.txt`, including GitLab entries. Legacy or previously pinned isolated HOME uses
   a symlink to that original registry, replacing the old generated filtered copy.
   Hole 2.1–2.3 use compatibility mode: each new session gets its own
   `$PIPER_HOME/sessions/<id>/hole-home`, and its generated settings live at
   `~/.hole/settings.json` there. An inherited `HOLE_SETTINGS_FILE` is removed. Existing
   sessions retain their pinned HOME, including across an upgrade to 2.4; a native-HOME
   conversation cannot resume on an older Hole and asks for an upgrade or a new session.
   Library/worktree exclusions follow the installed version: before 2.4 they require each
   library's own settings. Preflight and inside-container checks remain enforced. This
   compatibility does not backport the Docker VMM fix; upgrade Hole if masks fail.
   Doctor and session startup require Hole 2.1.0 or newer; 2.4+ is recommended.
3. **Spawn:** ptyhost runs `hole start claude <project> --library <session cfg>:~/.claude:rw
   -- --mcp-config …` on a PTY with input **locked**. piperd never passes
   `--trust-project`; hole's trust question is answered by you, in the terminal.
4. **Attestation (L4, when enabled):** after `Attaching to claude agent...` piperd checks the agent and
   DinD containers twice.
   - Docker's view (`docker inspect`): every candidate has an over-mount, `~/.claude` is
     the session's own directory, and every bind source is expected and none forbidden.
   - Inside each container: a script checks that no candidate and no exclusion is readable
     (it is a mountpoint and empty, or absent). This is the check that catches K18.
   - Plus: no secret-looking env (and no git repository, but only when `.git` is hidden
     again with `force_exclude: [".git"]` - git is open in the sandbox since 2026-10-01).

   With verification off, input unlocks as soon as hole runs; it is not reported as verified.
   With verification on, only then is input unlocked and the task typed in. A failure stops that session's
   `hole` process (never `hole destroy`, which kills all sandboxes of a project).

The **Global agent** mounts no project. It reads checkouts through `list_dir`, `read_file`
and `grep`, served by piperd on the host (`projectfs.py`) under the same hiding rule as the
project's own session. So it has no directory for hole to hide and starts even under K18.

Piper's tools (git `git_status` / `git_diff` / `git_log` run on the host with policy files withheld, the
Global agent's tools, the [GitLab and GitHub tools](INTEGRATIONS.md)) go through `sandbox/piper-mcp.py`, then
a mailbox in the session's own config directory, then piperd. Writes (typing into another session, stopping
one, a forge comment or issue) wait for confirmation when their Security approval switch is on; otherwise
they are approved automatically and audited.


**Git in sessions:** Security › **Guarded git** controls read-only libraries and the sandbox Git guard.
With it off, Git behaves like Hole. Security › **Other projects need an unlock** controls session locks;
Git needs no unlock in Hole mode. Approval switches control `git_run` in the own project and in another
project separately, including ordinary reads. **Harden host git**, **Hidden files stay hidden** and
**Protected branches** are separate guards; Git settings tune them only while they are enabled.

When enabled, the **L5 watcher** rescans its mounts every 30 s
(`PIPER_WATCH_INTERVAL`; the advisor's `$PROJECT_PATH` every 2 min). A sensitive file that
appeared after the start is not over-mounted, since hole expands globs once. The watcher
marks the session *attention* and lists the file, or stops the session when `policy.json`
says `"l5_action": "stop"`.

## The GUI

| Key / control | What |
|---|---|
| Layout | **activity bar** (Overview · Agent ⌘0 · Host shells ⌥F12 · Usage · Time · Audit · Stacks ↗ · Settings ⌘, · Keys) · **queue** · the **detail** of what you opened · the **context panel** (⌘I). The queue hides for Audit, Usage, Timesheet, Settings and the grid |
| **Queue** (Overview) | one list of what wants you: **Needs you** (L5, permission prompt, confirmation with Allow y / Deny n, failed or blocked start, a session waiting for you, a stop that hangs with piperd's KILL countdown; blocking first, then the oldest) · **New** (GitLab assignments and mentions, production errors, failed CI) · **Running** (⌘1–9; idle ones marked) · **Recently** (the Feed's events, unread dotted; needs the Feed) · **Ended** (Continue, ✕). Compact Overview toolbar with standalone **⌘K**, **+ Session** and collapse; search stays visible below. Flat rows show title, age and project/issue metadata without last-message previews or workflow next-step labels. No source filters; GitLab and Production have their own activity-bar views. piperd sends the open actions in `/api/state` `queue` (`feed.derive_actions`) with or without the Feed. The Overview icon hides the queue (a wide terminal), shows it again; **⌘B** (or the fold button in its header) folds it to the **strip** of project tiles and back - one mode for every view, the strip by default; going into Files or HTTP folds the whole queue to the strip, nothing else changes it. The strip: a tile per live session (project initial + #issue / project digits, project colour, the state as a corner badge, ⌘ held = ⌘1–9 numbers, hover = title · project · since · what it does), the top button opens the whole queue. A tile, a session row of the queue and ⌘1–9 all switch the session and keep the tab (Files / HTTP follow its project, Conversation / History show its own; the Global agent opens its Terminal). ⌘1–9 number the strip's tiles from the top while it is shown, Running otherwise. More tiles than the height fold into "+N" (= the whole queue). Tile colours follow the projects' order in the configuration. The whole queue's right edge drags its width (280–560 px, double-click = default) |
| queue keys (while it has the focus) | j k ↑↓ move (the order holds still while you move), Enter / o open, **r** reply to a session (the collapsed Composer), **y / n** allow / deny, **e** dismiss (Feed; GitLab: mark read), **m** mute 1 h (Feed), **a** hand to the Global agent (Feed), / search, Esc back to the terminal. **r** opens the Composer collapsed and pinned to that session. **⌘⇧L** puts the keyboard into the queue from anywhere, **⌘J** opens the next item that needs you |
| ⌘K (or **⌘K** in the queue header) | command palette: switch (waiting sessions first), views and actions, stop, resume, advisor, new session in a project |
| ⌘1–9, ⌘0 | switch to session 1–9 of Running; ⌘0 = the Global agent (its chat drawer when the Feed is on, else its terminal; started if it does not run) |
| drag a session in Running, ⌘⇧↑ / ⌘⇧↓ | reorder the running sessions; the order is remembered in this browser (new ones join below: the Global agent, then the oldest). ⌘1–9 follow it. Palette "Running: default session order" resets it |
| queue: right click on a session (or ⋯ in its header) | open, add to / remove from the grid (grid only), Files / Conversation / History, pin to the top (stays there even when it ends), move up/down, rename, stop, resume, remove |
| queue: blue dot | the session finished a turn while its terminal was not on screen (or an unread event); gone once you open it |
| ⌘\\ | split = the two-pane grid preset: the active session and the previous one (needs the grid module) |
| ⌘I (◧) | context panel of the session: **Needs you** (confirmations, failed attestation, L5), then sections with one header line each - title, state (`5 of 11`, `1 error`, `clean`, `3/3 up`) and actions; a body opens when there is something to do, a click on the header folds it (kept per section): **Issue** (workflow bar, comment draft **review and send**, **↻ Update issue status**), **Last turn** (your prompt, the end of the answer as plain text, text held back with **Send now**, the turn's files and Full diff), **Changes** (branch, CI/MR, the commit list with OS files and `development/` folded, commit, push), Diagnostics, Stack, **Isolation** (actual checks/configuration and effective unlocks; hidden when inactive), **Network** (including refused domains and provider scope), **Session** (session data). Off by default under 1400 px (⌘I remembers the choice); hidden on the global views |
| session header | title (double-click renames), project, issue ↗ (opens it in the GitLab view), branch, isolation; Stop / Retry / Close, ⋯, ◧; tabs **Terminal · Conversation · Files · History**, Single / Grid on the Terminal tab, Composer ⌘E; the state in words (WAITING FOR YOU · since · changes · tokens). **Conversation** opens the Composer collapsed under it for a live session (Enter sends; while Claude works it is queued until the turn ends); ⌘E switches collapsed ↔ full, Esc closes it until you come back |
| ⌘F | search in the terminal (the canvas is invisible to the browser's own find) |
| Shift/Option+Enter | newline in Claude Code (`ESC CR`); Option stays Option (Czech keyboard) |
| Esc, Ctrl+K, Ctrl+C | go to Claude Code, never to the GUI |
| paste / drop an image | stored in the session's `~/.claude/piper-drop/`, its path pasted into the prompt |
| `path/File.java:123` in the output | a plain click opens it in Piper's editor (the Files pane next to the terminal); ⌘ / ⇧ click (or a path outside the project, or the Global agent's terminal) opens IntelliJ (`idea://`); the project has the same path in the sandbox |
| Git panel | every changed file of the whole repository (files hidden from Claude marked with the eye and unchecked; **Open** / **Diff** / **History** on each row, a line number in a diff opens the file in Piper's editor), diffs; **Checked files** commits the checked working files, **Git index** offers Staged / Unstaged with Stage / Unstage (also all) and commits staged bytes while keeping unstaged edits; push after a confirm. A `core.hooksPath` inside the working tree (husky) is not run: the agent can edit it |
| Activity bar | Overview (badge: what needs you; always opens the whole queue), Agent, Host shells, then **Usage · Time · Audit · Stacks ↗ · Settings · Keys**. Stacks = the dev stack provider's own dashboard (when an extension supplies one) |
| Sessions list | compact Overview header with standalone `⌘K`, `+ Session` and collapse; always-visible search below. Bold title, project/issue and age; no source filters, last-message preview or workflow next-step labels. Ended starts folded; search also finds ended sessions. Permissions and errors remain visible |
| **Files** tab | the whole project (only `.git` never); files **hidden from Claude** (isolation policy / excludes) open, search and edit like any other, marked with the eye icon and a banner - sessions still never see them. The queue folds to the strip of session tiles (⌘B = the whole queue). **Tree**: scope All / Changed / Turn / Open, typing filters, git letters and change counts, tooling folders folded ("+ N system"; ⌘P / search leave the same folders out, "Search the excluded folders too" widens it), git letters also without a session, right click / ⌥N ⇧F6 F6 ⌫ = new, rename, move, delete (to Piper's trash, Undo) - also while the session works, except a file Claude edited in its current turn; a file hidden from Claude appears while a session runs only inside a directory its sandbox over-mounts (elsewhere the sandbox, started before it, would see it). **Editor** (CodeMirror 6): always editable, **also while the project's session works** (a save over Claude's newer edit is refused: Reload / Overwrite); ⌘S saves with the sha lock; imports folded, sticky class/method, change marks + Claude's last edit (purple) in the gutter, inline blame, IntelliJ problems as squiggles; when the session edits an open file it reloads and flashes the lines. One-line header: path › class › method and icons (@ = `path:line` into the prompt, ⌥⏎; ± diff ⌘⇧D; blame; history; outline; IntelliJ). **⌘P** searches files, #symbols, >actions, /text (empty = recent locations). Bottom panel (⌘⌥↓): Usages ⌥F7 · Search ⌘⇧F (git grep) · Problems · Diagnostics · Turns (this file's changes per turn); F4 / ⇧F4 step |
| ⌘P / ⌘⇧F | open a file by name (`Foo.java:120` jumps to the line; empty = recent files) / regex search in the project (package-manager caches like `.pnpm-store` skipped) |
| tree + preview | compact packages (`src/main/java` → `com.acme.shop`), dirs first, changed files marked; syntax highlighting (Java, Kotlin, TS/JS, XML/HTML, JSON, YAML, properties, SQL, shell, Python, CSS, Markdown; own lexer, no innerHTML) |
| click on `path:line` in the terminal | opens it in the **Files pane** next to the terminal (⌘ / ⇧ click opens IntelliJ); paths in the git panel, review, history and the conversation open the editor too |
| **Files pane** ⌘O | the file browser as a grid pane next to the terminal: tabs of open files (⌥W closes, middle-click too), the tree on demand (≡), drag the gap to resize. ⌘P from the terminal opens the file there; ⌘D adds a Database pane of the active project. Without the grid module a path opens the Files tab |
| In a file | ⌘F find (Aa / W / .*), ⌘L go to line, ⌘⇧O outline, ⌘+click (or ⌥+click) = go to definition (regex symbol index, ⌘[ back), ⌥F7 find usages, `#Name` in ⌘P = symbol search. Gutter marks changed lines against HEAD; **Diff** = side by side; **Note** adds a review note to the Git panel; Markdown has labelled **Preview / Edit** modes above the editor; images are previewed |
| **Edit** (⌘S) | small edits from the GUI: saved only if the file did not change since you opened it (sha) - also while a session works; eol and final newline kept; a file hidden from Claude is rewritten in place (its over-mount stays) with a crash journal, never when a running sandbox does not cover it; audited (`gui-write`, `hidden` for a file hidden from Claude). Create / rename / move never put a hidden file's content where a session sees it, and, while a sandbox of the tree runs, create a hidden path only inside a directory its sandbox over-mounts (its over-mounts were fixed and pinned at its start). A file over 2 MiB or a binary one says so (413 / 415) |
| **.http files** | IntelliJ's HTTP Client format, run by JetBrains' `ijhttp` in a dedicated OS sandbox with the real `http-client.env.json` (+ `http-client.private.env.json`) - the sandbox never sees them. Environment picker (green dev / orange test / red prod, remembered per project); **No environment** runs literal URLs with production confirmation and verified TLS (never remembered), **▶ Run all**, ▶ next to each request (runs it alone with the globals the last run set, e.g. a `sessionId`), results below: status, time, `client.test` ✓/✗ with the failing line, a scrubbed **Script log** for native pre-request, response-handler, imported-module and test-callback `client.log`, including state/logs before `client.exit`, headers (Authorization/Cookie masked, **show secrets**), the body formatted, earlier runs (yours and the sessions'). Production asks once more, and once more for requests that change data. **Share bodies with sessions** decides whether `http_last_result` gives a session the (scrubbed) bodies - on for dev/test, off for prod. **Settings → HTTP requests** sets the total, connection and socket timeouts for subsequent runs. Native `run`/`import` keeps nested file scopes and overrides, including repeated and local named requests; results retain source links and distinct occurrences. Production confirmation expands imported methods/URLs and pins the dependency snapshot. Original response redirects and `save_to` preserve scripts/tests and exact binary bytes in the supported CLI revision, without refetching; a hash-pinned local compatibility overlay leaves the installed CLI untouched. Collection exports retain distinct response bytes; common script blocks work in private inline/external scopes. Globals persist across files and Run all within the same physical project/env source/environment. Runtime body-file paths, including exports named by a response handler or test callback, use a project-bounded broker and, for agent runs, a concrete one-shot file approval; declining stops that native run without replay. User-owned private environments support client TLS certificates in PEM/DER with a key, or P12; encrypted identities use a mandatory one-run passphrase card without reading keychains; the snapshot and TLS settings are pinned to the approval, and explicit server verification is preserved. Cookies persist across files and runs in that same scope with Domain/path/expiry/Secure/host-only protections and redirect handling; agent sandbox relays have a separate cookie scope per session, preserving their own login across files while keeping the host/GUI login separate. Stored credentials stay masked and privileged scripts require approval, including hidden private-only environments. Automatic tunnel TLS exceptions apply only to the rewritten loopback endpoint, including redirects; explicit user TLS settings remain effective. Computed imports, variable pre/response script paths and executable response exports use a complete immutable module graph and a mandatory one-shot code approval for GUI and agent runs; common scope and relative imports stay native. Managed OAuth supports Mock, Client Credentials, Password, refresh, Authorization Code with optional PKCE and a configured loopback callback, and Device Authorization. Browser sign-in has a mandatory one-run card; opening the provider does not approve an unfinished login. Finite SSE streams preserve native line/finish callbacks, late tests, split UTF-8, following-request globals and cancellation without editing the source. Some browser-only and protocol features of the IDE client are not supported. Scripts cannot read the host home, start child processes or contact destinations outside the run’s approved host/port set. Linux needs bubblewrap and libseccomp; confinement failure stops the run. No `ijhttp`: **Install** downloads it into `~/.piper/ijhttp` (needs Java 17+) |
| **HTTP** tab (session) | every `.http`/`.rest` file of the project in one list, grouped by directory, filterable by path, request name or URL, with how its last run ended (yours or a session's); opens the file run last; a file hidden from Claude is listed too, with the eye icon. For the picked file: environment, **▶ Run all on <env>**, the requests as rows (▶, method, name, URL resolved for the environment, status of the last run). A click (or Enter) unfolds a request: its last result (tests, headers, body), the request as written, **▶ Run** and **Open in Files**; double-click or `o` opens it in Files, `r` / ⌘Enter runs it, ↑↓ move. Production turns the run controls red and is never remembered (preselected only when it is the only environment); results of another environment are dimmed with "results are from …" |
| **Database** | activity bar **Database** or the session tab: the project's connections (dev from the project's dev stack when an extension provides one, test/prod through a tunnel Piper opens, class colour first), the object tree, table tabs (WHERE, sort, columns, FK jump ›, inline edit → pending changes **Submit ⌘S** / **Revert Esc**, + Row, ⌘⌫ delete, CSV import, copy as CSV/INSERT/JSON, DDL), SQL consoles (⌘↩ runs the statement at the caret, ⌘E explain, completion from the schema, **Read-write** = one open transaction until **Commit ⌘⇧↩** / Rollback; **Own tx / Shared** chooses separate consoles or one shared transaction for this connection in this DB view, with Commit all / Rollback all), Claude's queries and the history on the right, **Connections** (add, password, tunnel, Test, reset pin, IDEA import). Raw data for you, no card; production changes ask once more in red. Your queries have their own timeout (Settings → Databases → Query timeout in the Database view, 300 s); a session's running read shows in its context panel's Database section with **Cancel**. **Agents** controls whether sessions can see/use a connection through tools or MCP: **Only you** keeps it in your GUI; new test/prod connections default to Only you, existing definitions keep their access. CSV imports and agents never join Shared console transactions |
| **Terminal (host)** | off by default: Settings → Terminal → **Host terminal (no sandbox)**. Then **⌥F12** (rail icon, session ⋯ → Terminal beside this session, or **Beside → Terminal**, ⌘K → Host shell: <project>) opens login shells **on your Mac, without a sandbox**, in the session's verified working directory; its terminal and Database beside layout are remembered per session. With Host shells off, Beside → Terminal opens its setting. Linked remote sessions use their remote Piper GUI for host shells. Tabs, `⌥⇧T` new, `⌥⇧W` close, a red **HOST** strip above. Only the GUI's own token opens one (never `piper attach`, a session or MCP); open / close / exit are audited, never the content |
| **History** / **Graph** | commits of a file or the repository with their diffs (files hidden from Claude included), **Blame** with age colours, the branch graph |
| Context panel: **Last turn** (last section, folded by default) | what Claude changed in one turn: piperd snapshots the working tree at your prompt and at the turn end (temporary index + `write-tree`, no branch/index/ref change); files and diffs per turn, older turns in the picker, notes on lines as in the review diff |
| Git panel: CI / MR | pipeline status of the branch (GitLab through piperd; the session reads the failed job's log tail with `gitlab_job_log`), a notification when it turns failed; **Create MR…** after a push (title/description drafted from the commits, you confirm) |
| Inspector: **Diagnostics** | compiler/test errors (mvn, javac, tsc, eslint, gradle, python, junit) from the session's last Bash output, grouped by file, click = open in the Files pane, "Send to Claude" sends `path:line: message` |
| Inspector: **Stack** | the project's dev stack from the stack provider (an extension): services, logs (filter, auto-refresh), restart after a confirmation |
| **Usage** | tokens per day (30 days) and per project from the Piper sessions' transcripts |
| **Feed** (off by default: Settings → Feed) | Its actions are the queue's items, its events the queue's **Recently** and its chat the **Agent drawer** (⌘0, ⌘.). What it adds to the queue: **cards** = what waits for you (L5, permission prompt, confirmation with Allow/Deny, failed start, a session waiting for you (at once) with a reply box, failed CI of a project with a live session, new assignment/mention in GitLab with the comment as a quote, new production errors; each disappears when its reason does, **snooze** / **mute 1 h** (both undoable: **undo** / **cancel**), security items neither), **grey lines** = what happened (turn ended, session created/ended, handoff result, new comments, CI passed again, "handled …"; folded per key within 10 min, a "new" line, "Since 14:05: …"), **bubbles** = the chat with the Global agent. The input always writes to the Global agent: the message is queued and typed into its terminal when it idles at an empty prompt (the handoff rules, never into a permission prompt); the chat is its own transcript, so terminal and chat agree. **→ agent** on any item adds only a reference (`(ref: gitlab p/x#12)`), never the notification's text. Agent not running: **Start and send**. A sent message stays visible until it is in the agent's transcript; if the agent ended first it is "not delivered" with **Send again**. ✕ cancels a queued one. In the drawer Enter sends, Esc closes it. piperd derives everything on its own tick (`feed.py`), `feed/events-YYYY-MM.jsonl` keeps `retention_days` |
| **Keys** (activity bar, ? in the queue) | all shortcuts, grouped |
| **Settings** ⌘, (activity bar, palette) | **Security**: Recommended / Minimal / Strict profiles, applied in two clicks. Customize details keeps the original tree and directly visible approval switches, with bulk controls and search. New installations use Recommended. Exact old profiles upgrade to their new versions; deviations and current-revision manual saves remain Custom. Other sections tune enabled guards and configure features, providers, projects and tools; inactive guard options link back to their switch. Each field says when it applies. Changes are audited, concurrent edits are refused (409), secrets stay masked, and only the GUI token may save. |
| **Grid** ⌘G | up to **9 panes** (layouts incl. 3 × 3): live sessions, **host shells** (red HOST frame, no sandbox; `⌥⇧T` in the grid opens one into it, ✕ keeps the shell running) and up to 3 **Database panes**, each with its own connection picked in the pane. **Drag the gaps** between panes to resize (the column gap and each column's row gap separately, double-click = half; remembered in this browser). Edit it in place: ◀ ▶ reorder, ✕ remove, the empty cell **Add to grid** adds a session, a host shell or a Database pane (layout menu **Choose panes…**, ⌘K **Grid: add …**); the pick is remembered in this browser. A click into a session pane makes it the active session (header, inspector, composer follow), ⤢ shows it alone (a shell in the Host view, a connection in the Database view) |
| two sessions in one project | allowed, in the same checkout and on the same git branch (no worktree). The second is named `<project> · 2`, the dialog warns, its Claude is told who else works there, and the Git panel says the change list is shared |
| diff → click a line | a review note (⌘⏎ adds it); **Send to session** (Git panel) sends all notes as one `path:line: note` message; a click on a line number opens the file there |
| confirmations | an item of Needs you (y / n); the card with the whole text (a diff of `edit_file`) floats bottom right for the item you opened, and for every request while the queue is hidden (grid, Settings…) |
| **Transcript** tab | the transcript as text, filter, token usage; palette: search the conversations of all sessions |
| **History** tab / Audit | `audit.log` of the session / of all of Piper |
| Network → **Allow** | adds a denied domain to `network.allow` of the project's `.hole/settings.json` (never the GitLab host); the next start asks you to approve the diff |
| **Composer** ⌘E | module, off until switched on in Settings › Features (off: Reply and DB "→ Claude" go to the terminal). Multi-line input with templates and history (this browser), ⌘Enter sends, **To all…** sends to every live session after a confirm |
| New session → **From an issue…** | the task is prefilled from the issue description and all its notes (GitLab: needs the piperd GitLab token, step 6; GitHub, for a project without GitLab: `gh auth login`) |

Notifications and the PWA badge fire when a session waits for you and is not in front.
Install the page as an app from Chrome ("Install"/"Open as window") for its own window.


## Layout

| File | Role |
|---|---|
| `piperd.py` | HTTP + WebSocket server (loopback only, token, Host/Origin checks, CSP), static GUI |
| `sessions.py` | start pipeline, state machine, hook events, mailbox, confirmations |
| `ptyhost.py` / `ptyclient.py` | the long-lived PTY holder (AF_UNIX, ring buffer, safe-point replay, input lock) |
| `guard.py` | policy, hole glob semantics, L1/L3 preflight, L4 attestation |
| `holehome.py` | personal hole settings → Piper's base |
| `sessioncfg.py` | per-session `~/.claude` from the template (+ hooks, MCP config, CLAUDE.md block) |
| `gitops.py` | host-side git: the GUI's full view, the session's filtered view |
| `projectfs.py` | the Global agent's read-only `list_dir` / `read_file` / `grep` over the checkouts |
| `mattermost.py` | notifications through a Mattermost incoming webhook (outbound only) |
| `gitlab.py`, `projects.py`, `digest.py` | GitLab proxy, project whitelist, transcript digest |
| `prodcheck.py` | the morning production check (schedule, the check session, `report_production`, novelty, mutes, mapping) |
| `timesheet.py` | the Timesheet view: attention blocks per issue |
| `feed.py` | Feed: actions derived from the state, events from snapshot diffs, the chat queue to the Global agent; off by default |
| `settings.py` | Settings: the settings registry, validation, atomic writes, the weakening confirmation |
| `dbtools.py`, `dbworker.py`, `dbsql.py` | the DB client: connections, cards, masking, audit, tunnels / the driver process (`~/.piper/dbvenv`) / SQL text rules |
| `sandbox/` | `piper-mcp.py` and `piper-event.py`, copied into every session config |
| `advisor/CLAUDE.md` | the advisor's instructions (copied to `$PIPER_HOME/advisor/`) |
| `web/` | GUI: `src/*.ts` → `dist/` (tsc only, committed), `vendor/xterm` (xterm.js 6.0.0, see `VERSIONS`) |

Environment: `PIPER_HOME` (`~/.piper`), `PIPER_PORT` (17895), `PIPER_MAX_SESSIONS` (6, only the default of Settings → Sessions → Concurrent sessions at most),
`PIPER_HOLE_BIN` / `PIPER_DOCKER_BIN` (test seams), `PROJECT_PATH`,
`PIPER_EXTENSIONS`, `PIPER_SERVICE_LABEL` (`piperd`), `PIPER_PROD_TICK` (60 s, 0 = no morning check loop), `PIPER_PROD_TIMEOUT` (1200 s), `PIPER_FEED_TICK` (2 s, 0 = no Feed tick),
`PIPER_DB_PYTHON` (the DB drivers' interpreter, default `~/.piper/dbvenv/bin/python`).

## Known limits

- **K18 / Hole 2.4:** excluded directories use a per-instance Docker mask volume, with tmpfs
  fallback on engines without volume subpaths. Piper accepts only this instance's mask volume
  and still checks content inside the container. Doctor's mount test checks the tmpfs fallback.
- Global excludes (including global profiles) also protect libraries and derived worktrees.
  Their own `<library>/.hole/settings.json` adds exclusions; project-only patterns do not reach them.
  Directory includes remain outside this rule and are scanned separately.
- With `claude_dir` `host` (K2 accepted) a session sees and can change your real
  `~/.claude`: transcripts of every session, hooks and settings that run on the host.
  `~/.claude` is then also left out of the L3 scan.
- **git guard**: what a sandbox writes into a repository's `.git` that git on the host would run (a config key
  naming a program, a hook, a `commondir`) is reverted within about a second, kept in `$PIPER_HOME/quarantine/`,
  audited (`git-guard`) and the session is flagged; your own changes while no sandbox can write that `.git` become
  the new baseline (`$PIPER_HOME/git-guard.json`). An IDE polling git inside that second is not covered.
- **Settings → Approvals** (`"auto_approve"` in `policy.json`): the confirmation kinds that run without a card
  (still in `audit.log`). Default since 2026-10-04: `git_commit` and `git_push` (git is open in the sandbox; a push
  whose commits delete hidden files is refused either way); every other kind asks. Skipping another card needs
  the confirmation every weakening does.
- **Host hardening** (Settings › Version Control › Git, › Sessions › Approvals and › Sandbox; on by default, turning off asks): `git_fail_closed`, `write_guard`,
  `stack_scope_project`. Each switch explains its restrictions in Settings. **Opt-in autonomy keys**:
  `protected_branches`, `mr_target_protected`, `settings_auto_tighten`, `login_spread`, `codex_in_claude`, `git_host_hint`, `git_run_projects`,
  `gitlab_extra_tools`, `feed_auto_actions`, `open_links_on_host`, `clipboard_arm_enter`.
- `"unrestricted_network": true` in `policy.json` (Settings › Sessions › Sandbox) starts every session with `hole -u`: no
  egress filter, so a session reaches GitLab and internal services directly and the
  allow-list shown in the GUI does not apply (the inspector says so). `~/.config/glab-cli`
  is never mounted into a session, with or without it; GitLab stays with piperd.
- Per session, the **New session** dialog has **Docker (DinD)** (`hole --with-docker`) and
  **Unfiltered network** (`hole -u`), each warned when ticked. They are kept for the session's
  life (Continue too), shown in the inspector and audited (`session-create` `opts`). Only
  `POST /api/sessions` / `/api/inbox/start` with the GUI token sets them (403 otherwise);
  Agent-started sessions never inherit project preferences; explicitly enabled automation start options
  continue to use their legacy global policy.
  The New session dialog remembers the **agent and start options per project**, including its worktrees.
  **Remember for this project** is on by default; untick it for a one-time choice. Preferences are saved only
  after a session is created, never on Cancel, and are shared by browsers connected to this Piper.
  Existing `start_opts_default` values remain a first-use fallback for projects without saved choices.
  A project that always needs Docker can set `container.docker: true` in its
  `.hole/settings.json` instead (approved in the diff like any other change).
- `git_commit` / `git_push` from a session run on the host after you approve the card. You
  approve the paths; their content is whatever the sandbox has there when git commits.
- Unlock per session (inspector → Isolation & network → Unlock, warned first, no Claude restart):
  **reading other projects** - `list_projects` / `list_dir` / `read_file` / `grep` over
  PROJECT_PATH, read-only, each project under its own hidden rule; **editing other projects**
  - `edit_file` / `write_file`, every write a card with the exact diff (not the own project,
  `.git`, hidden files, symlinks); **full git** - `git_run`, any git command on the host
  checkout, no filter, every call a card with the exact command (hooks off, no editor).
  Locked, the tools answer so before any card.
- A session knows it runs in hole under Piper (a "Where you run" block in its CLAUDE.md /
  AGENTS.md): `piper_session` shows its own sandbox (allowed domains, hidden paths, unlocks),
  `piper_settings` the settings it may propose, `piper_settings_change` / `piper_allow_domain`
  propose a change under the corresponding Security approval switch (on by default). They cannot
  change Security, secrets, the environment, the project table or the GitLab host.
  Settings › Sessions › New sessions → **Language of the answers** (e.g. `Français`) is added to every session.
- Handoff: a project session may `propose_session(project, task)` in another project. You
  read the whole task on the card and open it or not (the Global agent's `spawn_session`
  asks the same way since 2026-10-05; Settings -> Approvals can turn that card off). The child reports with `report_result`; piperd adds git facts, types only a
  fixed line with ids into the parent when it is idle, and the parent reads the result with
  `handoff_result`. One level only: a proposed session cannot propose another. The summary
  is the other agent's text - the parent gets it as data, but it does cross projects.
  The line is typed and submitted as soon as the parent idles at its prompt (`waiting`, or
  `attention` from an `idle_prompt` notification - never a permission prompt) **and** you have not
  touched the terminal after the Enter of your last submitted prompt (ptyhost `key_at` /
  `enter_at`; no guessing what an Enter did). Otherwise the card shows "⌨ … will wait" with **Send now**. A sandbox could forge the
  "prompt submitted" hook and let the fixed line join your half-written text in that same
  session; an older ptyhost (before `task piper:stop -- all`)
  cannot tell, and then the line is typed as before.
- A session may write a different but valid login into its `~/.claude/.credentials.json`.
  It is copied to the template when the session ends, so later sessions would run under
  that account. This is a residual risk.
- `~/.m2/repository` stays read-write (builds need it) and is not scanned
  (`scan_skip_roots`). A sandbox can plant a plugin JAR there that a later **host** Maven
  build runs.
- The network inspector parses hole's `-n` dump by the words ALLOWED/DENIED. The file
  format is hole's, not a contract.
- The dashboard self-test covers the Sessions button only in its hidden state (the fixture
  has no `piperUrl`).
