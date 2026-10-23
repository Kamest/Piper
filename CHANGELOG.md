# Changelog

Notable changes per release. A release whose notes say **ptyhost changed** needs `task piper:stop -- all` and
`task piper:open` once, which ends every running session. Every other update only needs `task piper:restart`, and
running sessions survive it.

## Unreleased

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
