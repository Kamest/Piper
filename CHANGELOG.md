# Changelog

Notable changes per release. A release whose notes say **ptyhost changed** needs `task piper:stop -- all` and
`task piper:open` once, which ends every running session. Every other update only needs `task piper:restart`, and
running sessions survive it.

## Unreleased

**ptyhost changed** (terminal reconnect and replay): run `task piper:stop -- all` and `task piper:open` once.

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

- Desktop: a Microsoft Store package (MSIX/appx, x64 and arm64) next to the unchanged NSIS installer. The Store
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
