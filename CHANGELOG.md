# Changelog

Notable changes per release. A release whose notes say **ptyhost changed** needs `task piper:stop -- all` and
`task piper:open` once, which ends every running session. Every other update only needs `task piper:restart`, and
running sessions survive it.

## Unreleased

- Desktop explains how to update a core that predates `piper update` instead of asking the user
  to repeat the unsupported command. Detection requires the old CLI's usage banner and exit code 2;
  other failures keep their existing diagnostics. Verification: desktop unit and core-update
  regression tests. The Machine E2E scenario includes the message and a screenshot; its execution
  is pending remote CI.

- Proposed project sessions can propose further sessions, forming chains of any depth within the
  concurrent-session limit. Each proposal follows the existing approval and runtime rules; results
  return to the immediate parent. Verification: session regressions for a four-session chain and
  a three-session MCP/PTY flow, plus the daemon handoff end-to-end scenario.

- Restart and service-update actions immediately show diagnostics that remain responsive without the
  daemon. Authenticated health checks have a 1.5-second request limit and a 30-second observation budget;
  the screen shows elapsed time, recent checks, explicit update refusals and unconfirmed or timed-out
  outcomes. A per-start service identity confirms even a fast exec restart. Check again is read-only;
  diagnostics exclude server error bodies, credentials and terminal content. Existing workspace
  renderers, drafts, focus and matching native terminal selections survive recovery without navigation
  or terminal input. Desktop serves the same view locally while the service is unavailable.
  Verification: Python and client regression suites, desktop unit tests and real Electron flows with
  agent-browser screenshots against Claude's OpenPencil design. This does not establish the cause of
  every native freeze. The desktop portion ships in Desktop 0.1.10; the web screen and instance identity
  require the matching controller update. The HTTP asset allowlist includes the restart stylesheet;
  a regression checks every stylesheet linked by the served shell through the real HTTP handler.

- Restarting Piper validates the core Python sources and refuses an unfinished merge before stopping
  the running controller. CLI starts/reinstalls, automatic update checks and the restart API use the same
  preflight; a rejected update does not consume its automatic restart attempt. Diagnostics contain source
  locations without source text or private paths. Desktop startup and restart waits count HTTP probe time
  inside their actual deadlines, preventing a nominal 30-second wait from taking two minutes.
  Verification: codebuild, CLI and restart API regressions; desktop runtime/service tests.

- Native Codex questions appear in Your turn independently of agent activity; opening one focuses its
  existing native terminal without typing or redirecting to Conversation, including attached-TTY sessions.
  Hook persistence failures no longer discard later events, retry in the background and show a history
  warning. Failed snapshots retain actual turn
  boundaries, mark the affected comparison unavailable and preserve access to other finished turns.
  Native authentication detection recognizes expired-token errors, ignores quoted code and clears
  stale warnings after later native activity; outstanding approvals and old hooks cannot clear them.
  **ptyhost changed:** terminal logs rotate at 16 MiB with one backup (`PIPER_PTY_LOG_BYTES`), and write
  failures are reported without losing live terminal output. Existing logs are not purged.
  Verification: `test_log_recovery.py`, `test_ptyhost.py`, session/direct-runtime/workbench/review suites,
  client regression suite, GUI build and `gui.py --log-recovery-only` at 1024/1920 px.

- Review keeps safe diagnostics for failed turn snapshots alongside their actual boundaries. A refused
  snapshot (for example, a fail-closed repository filter) is recorded per session and the lane explains
  the reason. Turns with missing snapshots cannot be compared or reverted; older affected turns read
  "snapshot failed". Error text other than the fail-closed refusal is
  reduced to its type. A running turn no longer claims "no files" before its end snapshot. The tab marks a
  clicked turn at once instead of leaving the previous comparison on screen. Edits by the agent no longer
  refetch and redraw a finished turn. Live comparisons refresh one at a time, with changes coalesced, and an
  unchanged result keeps its nodes. Verification: `test_turnreview.py` (failure attribution, path-free
  reasons, open-turn summary), `turnreview.test.mjs` (click feedback, pinned cache, coalescing, no stale
  session render), and an agent-browser regression with four distinct turns, including one with 600
  files and about 18,600 lines. One render of that turn still blocks a frame for about 0.3 s, now once
  per selection instead of on every edit. An edit of a file in the shown finished turn refreshes only its
  file versions, which are used for the line-note check, and the diff is not redrawn.
- Raw turn snapshots no longer re-walk every path of a large repository. The byte-exact snapshot, taken at
  every turn end and by a live Review comparison, walked each component of every path and opened every
  file even when its cached blob still matched. A repository of about 35,000 files took 25 s per turn,
  and over a minute after a daemon restart. Directories are now opened once along the path-ordered list,
  still without following links. An unchanged file is only lstat-ed. A blob already in the session's
  store is hashed but not compressed again. A full blob cache drops its least recently used tenth instead
  of every session's entries. A Review diff above 1 MiB is filtered only up to the truncation point.
  Measured inside the test sandbox on that repository: 25 s → 3 s warm, 74 s → 9 s after a restart, and
  the All-turns diff of 3,272 files 10.5 s → 4.2 s. Verification: `test_gitops.py` (no file opened and
  each directory walked once for an unchanged tree, a same-size edit read again, a directory swapped for
  a symlink mid-scan refused, no stored blob compressed again while CRLF bytes stay exact, LRU eviction,
  the bounded filter equal to filtering everything and then truncating) and `test_turnreview.py`.
- Review answers each request within about a second on a large repository. All turns and a running turn
  took a fresh snapshot of the whole tree on every open and every refresh. They now answer within 0.8 s.
  When the snapshot takes longer, the previous comparison comes back marked `pending`. The tab shows it
  at once and asks again; the poll joins the computation already running, so an open tab never
  snapshots in a loop. A comparison finished within the last 2 s, with no agent edit since, answers as
  current. A failure is never hidden behind an older result.
  - The end of a turn prepares All turns for the next open, in its own thread, so the next prompt's
    start snapshot never waits for it. After a daemon start, the recently active sessions are prepared
    from their last finished turn, in the background and without a snapshot.
  - The last All turns comparison of a session is saved in its private directory and served as
    `pending` after a daemon start. The first open after a restart took 93 s on the live 3,272-file
    session, computed cold behind the start-up preparation of other sessions. One gzip file per session
    (about 0.5 MB for that session), overwritten; never kept above 8 MiB, after a failure, with the
    session's snapshots, or 7 days after it was written (removed at daemon start or when opened).
  - A long comparison shows its state: with nothing to show yet, a spinner and "Comparing all turns
    with the working tree…" (a turn: "Loading turn N…"); while a previous comparison stays on screen,
    a spinner and "Updating…" next to its title until the fresh one arrives.
  - Turn snapshots no longer keep pinned objects twice. Each snapshot left its new blobs loose and
    pinning packed them again (311 MB of the live session's 483 MB were such copies). Pinning now drops
    the loose copies the session's own packs hold, a later snapshot does not write them back, and a
    daemon start cleans existing sessions and removes temporary files a crash left behind (older than
    a day).
  - File lists, counts, versions and diff texts of compared trees are cached by tree id; trees are
    content hashes, so the cache never goes stale. Live file versions are re-read only when a file's
    inode, size or times changed.
  - The independent git reads of a comparison run in parallel.
  - git is stopped once the shown 1 MiB of a diff is read, instead of writing the whole patch.
  - A raw snapshot rewrites only the entries whose bytes differ from `git add`'s.
  - Measured inside the test sandbox on the 3,272-file session: opening All turns answers in 0.81 s
    with the prepared comparison marked `pending`; the fresh one follows after the live snapshot and
    comparison, about 2–3 s later (about 9 s after a daemon start, 40–50 s for a session's first
    snapshot into an empty store). Before, every open computed it fresh, 25–128 s. Its diff 0.15 s
    (was 12 s). Selecting a finished turn takes 0.8–0.9 s, mostly `git status`; its diff 0.02–0.07 s.
  - Verification: `test_turnreview.py` (turn-end preparation served pending behind a slow snapshot, no
    snapshot loop for an open tab, failure not masked, daemon-start warm-up, the next prompt's snapshot
    not delayed by the warm-up, a finished turn reusing its tree comparison while re-reading live
    versions, the saved comparison after a restart and its retention), `test_gitops.py` (bounded diff equal to the
    truncated full one with hidden blocks withheld, no cut line, minimal `update-index`),
    `turnreview.test.mjs` (a pending result drawn at once, then replaced, and polling stops), and the
    agent-browser regression (All turns with 602 files opens in 0.6–0.9 s).

## 0.1.5 and Desktop app 0.1.9 (2026-11-04)

- Desktop: About Piper Desktop (application menu and tray) shows the desktop version, OS and architecture,
  the running Piper service version, project links and safe copyable details. It remains available while the
  service is offline. Check for Updates opens the same window with progress and an explicit current,
  available or retryable error result. Unsigned macOS builds explain manual replacement; supported automatic
  updates wait for Download, show progress and offer Restart to Update only after the download completes.
  Microsoft Store builds open Store updates. None of these actions restarts the Piper service.
- `/api/state` exposes `service_version` from the core release tag and commit distance captured at daemon
  startup, separately from the unchanged API protocol `version`. Missing release metadata is reported honestly;
  an older running daemon is never relabelled after its checkout changes. Tests: `test_codebuild.py`,
  `test_piperd.py`, `desktop-updates.test.mjs`, `desktop-theme.test.mjs` and the real Electron About scenarios
  in `desktop/test/e2e.mjs` (including agent-browser interaction and screenshots).

## 0.1.4 (2026-11-04)

- Host file writes using a base project's `.claude/worktrees/<name>/…` path resolve to the validated
  linked checkout before checking busy sessions. A session in a sibling worktree no longer blocks
  that write; active sessions in the target or covering its parent still do, including after approval.
  The approval names the actual worktree. Tests: `test_agent_write_worktree.py`, `test_workbench.py`.

- Empty-task sessions acquire a short name from their first actual request, with a later unambiguous
  issue/MR reference as a prefix. Claude's automatic title can refine the name once; explicit CLI and
  Piper renames retain priority. Names persist across restarts and match the conversation picker.
  Quoted logs, injected context and ambiguous references are ignored; naming never attaches an issue
  or changes access or worktrees. Verification: session/digest regression tests and the isolated
  agent-browser `gui.py --auto-title-only` scenario for Claude and Codex at 1024/1920 px.

- Review stays in its session tab: Overview keeps the original Your turn / Running / Later placement and
  order, regardless of pending reviews. History checks snapshot refs and objects in batches, migrates legacy
  refs together and reuses saved turn statistics. The tab shares in-flight requests, loads history and the
  comparison concurrently, and caches recent comparisons until file/state changes or a 15-second refresh.
  Verification: `test_turnreview.py`, `turnreview.test.mjs`, `queue.test.mjs`, `test_workbench97.py`; the
  agent-browser regression opens a 120-turn history in under two seconds and captures Overview and Review.

- GUI: a one-time "What's new" screen (since 2026-10-13: desktop app, phone client, look, cards, review, agent
  switch, restore, updates) for a Piper home that finished the setup and was in use before the first open-source
  release (first audit record before 2026-10-23). A new home never sees it (finishing the setup marks it seen); it
  never opens next to the Security screen, the restore card or the setup, and waits for the next GUI open instead.
  Shown once, `seen` kept by piperd. Its header has buttons to the desktop app downloads on the
  project page and to the GitHub repository; the Desktop app card links the downloads, the phone card piper.kamest.dev, the footer the changelog. Tests: `test_doctor.WhatsNewNoticeTest`, `whatsnew.test.mjs`.

## Desktop app 0.1.8 (2026-11-04)

- Copy bundled Python with relative links intact, so the runtime remains independent of the signed
  application and Linux AppImage mount. Repair links in existing copies without replacing binaries
  used by a running service. Python checks and extension discovery disable bytecode writes into the
  bundle. Tests: `desktop-runtime.test.mjs`, desktop unit and Electron e2e. Verify the macOS application
  signature after launch when installing the update.

## Desktop app 0.1.7 (2026-11-04)

- Desktop: system light/dark changes no longer raise an uncaught main-process error on macOS, including
  while the setup window is hidden after connecting. Update the title bar overlay only when Electron
  provides its Windows/Linux API; the window background and tray still refresh on every platform.
  Regression: `tests/piper-unit/desktop-theme.test.mjs`; real Electron theme switching:
  `desktop/test/e2e.mjs`.

## 0.1.3 (2026-11-03)

- `piper update` fast-forwards the checkout (`git pull --ff-only`) and restarts a running piperd onto it, with
  sessions kept. With local changes or another branch, it changes nothing and says so. Test: `test_piper_cli.UpdateTest`.
- GUI: opened as `?window=N` (a desktop app window), it keeps its own last session, layout and grid; settings and
  logins stay shared. Test: `environment.test.mjs`.
- Resume conversation grows with a big window: at least 1160 x 860, otherwise 80 % of the window's width and 88 %
  of its height (a fixed size was a third of a large monitor). Test: `convo.test.mjs`.
- Database: a test / prod connection on localhost (your own port-forward) is saved; the editor warns that Piper
  trusts whatever listens there, and the server pin still refuses a different database later. Tests:
  `test_dbtools.py`, `db.test.mjs`.

## Desktop app 0.1.6 (2026-11-03)

Update Piper and per-window sessions need core 0.1.3.

- Confirmation cards are answered outside the window. A notification for a card has Allow / Deny
  buttons, and one for a waiting session has a Reply field (macOS, where the build may post native notifications).
  Each card in the tray menu also has an Allow / Deny submenu. Allow is offered only where the GUI's one-key allow is
  (no red card, no card that asks for text or an "always" choice), and Deny is always offered. Tests:
  `desktop/test/hands.test.mjs`, e2e against a real piperd (`allow_domain` cards, audit shows the answers).
- The tray's Machine menu has Start Docker (Docker Desktop, OrbStack or a Podman machine), Start / Restart
  Piper, Update Piper and Copy Diagnostics (`piper status` + `piper doctor`, with tokens and passwords removed). It
  works while piperd is down. The app runs these through the service's own commands and never owns piperd or Docker.
- More windows. File › New Window (⌘N) and Open Session in New Window put each window on its own session,
  layout and grid, for example a second monitor. They come back in the same place after the app restarts, or on the
  main display when that monitor is gone. The GUI keeps its last session, layout and grid per window (`?window=N`).
  There are no new GUI buttons.
- "Open in IntelliJ" (`idea://open?file=…`) reaches the OS again. Other `idea://` commands stay blocked.

## Desktop app 0.1.5 (2026-11-03)

- Notifications come from the app itself: the main process watches piperd's state (every 5 s) and announces a
  session that turns waiting / needs attention / waiting for trust and a new confirmation card, with the window
  hidden or closed too (a session set aside for later stays quiet, as in the GUI). A click shows the window on that
  session. While a Piper window has focus the GUI notifies as before; the GUI's copy is not posted twice.
  macOS refuses notifications to the ad-hoc signed build ("Notifications are not allowed for this application",
  Electron 45); the app then posts them through `osascript` (shown as Script Editor, a click does not open Piper).
  Renderer notifications (inbox, CI, production) use the same fallback. Tests: `desktop/test/notify.test.mjs`,
  e2e transition + click with the window hidden, and the click route into the real GUI (`#session=`).
- Tray: the number counts only what blocks work (confirmation cards, permission / trust, blocked, security). Waiting
  sessions and the inbox (forge, CI, production) are separate menu rows ("4 sessions waiting", "27 in inbox").
  Test: `policy.test.mjs`.

## 0.1.2 (2026-11-03)

**ptyhost changed** (v9, terminal reconnect and replay). `task piper:restart` is enough: sessions survive and
piperd works with the running ptyhost. The new replay arrives after `task piper:stop -- all` and
`task piper:open` once, which ends every running session - do it when convenient.

- Extensions without commands: Piper loads every trusted extension itself (no wrapper or service flag needed, one
  service name `piperd`), Settings › Extensions lists what was found with Load / Turn off / Add folder, a first-start
  card offers a found extension, a phone or remote browser can only request loading (approved on this computer),
  and an extension whose git origin changed stops loading until trusted again. Manifest API 2 adds `description`,
  `env_file` (parsed, never sourced), `service_env` and `legacy_labels`. The first bare `piper` in a terminal asks
  once; `piper extensions decline <path>` records a "no".
- Installer: an explicit terminal "no" to "Load extension …?" is remembered and not asked again (also by `--yes`);
  without a terminal nothing is recorded.
- Updates finish by themselves: after `git pull` the next `piper status` / `open` / `ensure` (or the desktop app)
  restarts an outdated piperd (sessions survive), the GUI shows "Piper was updated" with a Restart button, and an
  install still running under an extension's old service name moves to `piperd` - at the next start of that old
  service too - keeping its python3, port, projects folder and other baked-in settings. An extension inside the
  checkout that the old service loaded is trusted once, without a question. `PIPER_NO_AUTO_RESTART=1` turns the
  automatic restart off. Service manager calls are bounded, so a held-back launchd job no longer hangs the CLI.

- Databases: a K8S reach over `kubectl port-forward` straight from a cluster kubeconfig
  (`<group>/<cluster>-<kind>[.<tag>].yaml` in `$PIPER_KUBECONFIG_DIR`, default `~/.kube/clusters`); Add connection
  gets Project and Kubeconfig typeaheads, Import from IDEA proposes the likely kubeconfig for a test/prod
  connection, the Database view opens faster, MariaDB 10.x connects and a K8S server pin survives a pod restart.
- Sessions: set a session aside for later (kept by piperd, the same on the phone); agent-created sessions inherit
  the runtime; the Global agent's runtime is configurable with Hole as the default.
- Terminal: ⋯ Redraw terminal and ⋯ Copy terminal diagnostics; a fresh terminal gets its modes before the replay.
- Phone: pairing with one QR code and desktop approval, steadier reading, keys and links, coordinated reconnect
  notices, no zoom into fields and full height after the keyboard closes.
- HTTP client: worktree-aware environments (a verified worktree's files, the main/own stack choice), consistent
  loopback targets, bounded worktree domains before host approval, ingress redirects restored, encoded cookie
  paths preserved; a tunnel skips a port an outgoing connection holds.
- Fixes: Claude limits are fetched right after a window resets; the inbox notifies only newly observed events;
  approved in-place writes are honoured and human Git actions are not blocked; `piper:restart` waits out
  launchd's asynchronous bootout. Commit from explains itself (hover help, hint line).

- GitHub Sponsor button: link the repository to [Kamest on GitHub Sponsors](https://github.com/sponsors/Kamest)
  through `.github/FUNDING.yml`. The public sponsorship page is available.

- README: lead with managing Claude Code and Codex sessions, explain the first session before the feature
  gallery and architecture, and clarify approval-profile defaults and Hole versus direct execution. Existing
  screenshots, downloads and reference links are retained. Verification: the sandboxed `test_public_content`
  suite checks local README links, heading anchors and media paths as well as neutral public examples.

- Sessions on SSH servers (Settings › Servers): a user-level service (`systemd --user`, no sudo, no public port)
  or SSH-only (tmux), Remote Hole, and local HTTP/database tools for a server session through a reverse SSH
  tunnel - credentials stay on this computer and access can be revoked. New session: host → project →
  Claude/Codex → Hole/Direct; Resume with history per host. "Run directly" next to Hole.
- Optional mobile access: pair a phone by QR code or a one-time link (60 s), encrypted iroh relay, manage and
  remove phones, optional Web Push notifications, touch keys for the terminal.
- Quiet window (on by default): a session asks for quiet for heavy work, the others finish what they are doing,
  confirm and wait; queue of requests, operator exceptions, Activity history; agent `quiet_*` tools only on the
  user's explicit request.
- Security profiles Minimal / Recommended / Strict and Custom (manual choices survive upgrades); Recommended asks
  before a push. Customize details with search; Isolation section for direct sessions.
- HTTP client: nested imports and `save_to`, exact binary exports, cookies and globals across files, client TLS
  certificates, OAuth (Client Credentials, Password, refresh, Authorization Code + PKCE, Device Authorization),
  SSE streams, Script log, timeouts; runtime code and files outside the project need a one-shot approval.
- Databases: Own tx / Shared console (Commit all / Rollback all), per-connection visibility for agents (new
  test/prod connections are "Only you"), worktrees see the main checkout's connections.
- Editor and Git: scratch files outside Git (⌥⇧S or ⌘K from anywhere), commit panel with Checked files / Git
  index, Markdown Preview / Edit, resizable panes; reviews compare and commit Git's clean/eol-converted content,
  turn snapshots keep raw bytes.
- Fixes: an "Allow similar for 1 hour" grant never answers a forced card (privileged or strict runs) or a scoped
  profile; remote installs carry the GUI's theme file.
- Fixes: a pending file-revert note waits quietly during a Quiet window or a login dialog (it filled the log);
  approval cards relayed from a server never offer an hour grant, and one-time cards show no Allow ▾ menu.

- CI: keep desktop build and signing artifacts for one day, and screenshots, Store packages and package
  manifests for seven days. Published release assets retain their existing lifetime. Both workflow definitions
  are validated with actionlint.

- Approval cards show what an action changes, reads or sends, collected on the host before the card appears
  (two-second budget, late facts fill in): for a push the branch, protection, commits, diffstat, base-branch CI and
  a secret scan of the diff; for test/dev database writes a rolled-back dry run with "N of M rows change" and a
  before/after preview (Security › Databases › "Dry run test writes before asking", on by default; never for
  schema changes or production); for production reads an EXPLAIN summary and personal-data column names, never
  values; for HTTP the host class, first request, body size and the names of secrets used. Facts that cannot be
  checked are stated. Evidence is written to the audit log next to the decision. Repeated allows suggest opening
  the rule; the Allow menu adds "Allow similar for 1 hour" (expiring, revocable grants).
- Push evidence uses the card's push plan for new/existing branch labels, shows commit hashes beside ellipsized
  subjects and uses a 24-hour checked time. Cards with evidence omit the legacy upstream-ahead summary (a new
  branch has no upstream count); outgoing commits still come from the local remote-tracking refs.
- Review: a session enters To review after each finished turn that changed files. The Review tab lists the turns
  with their diffs and line notes, sends notes to the agent and marks the review done. Revert to here restores the
  session's files to the end of a turn without touching commits, HEAD or the index, saves the current files first
  (Undo / Restore), refuses while a turn runs and tells the agent which files changed. Turn snapshots are pinned
  under `refs/piper/turns/` in the session's private repository so Git GC keeps them.

## Desktop app 0.1.4 (2026-11-03)

- When it starts the stopped service itself (no terminal; at app start and from the offline screen's
  Start Piper), a found extension is offered first on the "An extension was found" screen; Not now is remembered
  with `piper extensions decline` before the service starts. Tray › Extensions… shows loaded / found / origin changed /
  incompatible / failed, lets several extensions be loaded at once and uses piperd's extensions API and self-restart
  while the service runs (the CLI otherwise). The build's discovery shim carries every name the
  discovery module imports (`APIS`, `ENV`); the build fails on a missing one.
- A Microsoft Store package (MSIX/appx, x64 and arm64) next to the unchanged NSIS installer. The Store
  build leaves updates to the Store and, after the reboot that enables WSL, continues setup on the next start.
  CI signs a copy with a throwaway certificate, installs it, starts it and checks the bundled Python in WSL.

## 0.1.1 (2026-10-24)

- `piper demo`: a small demo project and a first session that reaches an approval card.
- Project page: a [security model](https://kamest.github.io/Piper/security.html) (what each layer covers, what it
  does not, the defaults of a fresh install) and a [comparison](https://kamest.github.io/Piper/compare.html) with
  similar tools, with sources. The guide and README no longer claim that a fresh install keeps `~/.claude` and
  `~/.codex` out of the sandbox or reviews `.hole/settings.json` changes: under *Minimal* both are switches you
  turn on. The project page lists the protections that hold from *Minimal* up.

- Installer: discover extension manifests and ask before loading their wrapper. `--yes` accepts registered
  extensions; scan results require a terminal answer or `--extension <path>`. `--no-extensions` skips them.
  Verification is covered by `test_install_sh` with a throwaway checkout, stub tools and terminal consent.

- `install.sh`: one-command install (`curl -fsSL https://kamest.github.io/Piper/install.sh | sh`). It checks the prerequisites, installs hole and Task after
  asking, clones or updates the checkout, copies the example hole settings, then runs the doctor and opens the
  GUI.
- Docs: a user guide for newcomers ([docs/GUIDE.md](docs/GUIDE.md)), the integrations (GitLab, GitHub,
  Mattermost, IntelliJ IDEA, the morning production check, Time, remote access) on their own page
  ([piper/INTEGRATIONS.md](piper/INTEGRATIONS.md)), an example `~/.hole/settings.json`
  ([examples/hole-settings.json](examples/hole-settings.json)), the real list of prerequisites in the README.
- Settings › Security speaks of the agent, Claude Code or Codex alike ("The agent asks you before").
- Project page: link previews with absolute URLs, a section menu on phones, readable without JavaScript, a
  video instead of the GIF, and a 90-second captioned walkthrough (`docs/media/tour.mp4`).

## Desktop app 0.1.3 (2026-10-24)

- The macOS app now carries a complete (ad-hoc) signature, so a downloaded build can be
  opened with System Settings > Privacy & Security > Open Anyway instead of being reported as damaged.
  Windows releases are ready for free signing through SignPath Foundation (off until the project is
  accepted; [code signing policy](docs/CODE_SIGNING.md)). Every release generates a Homebrew cask and winget
  manifests and validates them.

## Desktop app 0.1.2 (2026-10-24)

The first desktop release ([desktop-v0.1.2](https://github.com/Kamest/Piper/releases/tag/desktop-v0.1.2), unsigned):
an Electron window and tray for macOS, Windows (WSL 2) and Linux. It attaches to a running service or runs a
first-run wizard (checks, extension consent, install, retry); quitting it never stops the service or sessions.
Desktop releases have their own `desktop-v*` tags; the core version is unchanged.

- Unit and Electron end-to-end tests run in the OS test sandbox on macOS and Linux; CI builds all six
  platform/architecture pairs and tests them, except Windows on Arm (its runner has no WSL).
- Each published release is installed and started on clean CI machines (`desktop-release.yml`).
- Not yet verified: a clean Windows/WSL first run through the wizard, signing and notarization, and updates
  from one desktop release to the next (`desktop/README.md`).

## 0.1.0 (2026-10-23)

The first public release. See the [release notes](https://github.com/Kamest/Piper/releases/tag/v0.1.0).

- One queue for every session (*Blocked*, *Your turn*, *Running*), approval cards with the exact command, a
  risk level and the rule that asked.
- Security presets *Minimal*, *Recommended* and *Strict*; every guard is its own switch, and what an agent does
  on its own goes to the audit log.
- The Global agent, which plans across projects and starts one session per project.
- Sessions that survive closing the browser and restarting the daemon (the terminals live in ptyhost).
- A terminal grid, a conversation view, Files, a Git window with worktrees and diffs, a database client with SSH
  tunnels, `.http` request runs, Codex ↔ Claude handoff, Claude usage limits, remote access over a tailnet and
  extensions for environment-specific behaviour.

Development notes for this release:

- The test sandbox runs in its own work directory, so CI calls the suites by `$GITHUB_WORKSPACE` paths.
  `scripts/piper-build.sh` uses a global `tsc` only when it is `TSC_VERSION`, otherwise npx with the pinned
  version. `svc_install` on systemd falls back to `id -un` without `$USER`.
- Examples use reserved domains and documentation IP addresses. Core imports are checked against the standard
  library and local modules; database drivers are allowed only in the isolated worker.
- The release revision passed `task piper:e2e` (unit, GUI node tests, `run.py`, `dbprod` with MySQL and
  PostgreSQL, `gui.py` with 804 screens). `http_e2e` was skipped on that machine (no `ijhttp`).
