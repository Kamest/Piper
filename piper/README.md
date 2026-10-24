# Piper reference

A local GUI to run several interactive Claude Code or Codex sessions, each in its own hole sandbox
with its root in one project, plus a **Global agent** (internally `advisor`) session that plans the work
and starts the project sessions. Sessions keep running when the GUI closes or piperd
restarts. An agent must never see excluded files. Nothing starts Piper unless you do.

This is the complete reference. New here? Start with the [user guide](../docs/GUIDE.md). GitLab, GitHub,
Mattermost, IntelliJ IDEA, the morning production check, Time and remote access are in
[Integrations](INTEGRATIONS.md). See [Architecture](../ARCHITECTURE.md) for the component boundaries and
[Security](../SECURITY.md) for reporting vulnerabilities.

## Quick start

1. `task piper:doctor` - what this machine still needs (hole ≥ 2.1.0, Docker, the Docker VM type, git, Python ≥ 3.10);
   every line that is not `ok` names its fix.
2. `task piper:install-service` - piperd starts at login (or `task piper:open` once).
3. `task piper:open` - the GUI. A new install shows the **setup card**: the same checks, and which parts of Piper
   you want to see (Basic / Developer / Everything; Settings → Features later).
4. **+ Session**, pick a project, write the task. The first session asks for `/login` once.

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
Fresh installs already allow commits and pushes automatically; changing network permissions
asks on an **approval card**. Allow or deny it in the GUI. Allowing it affects future
starts of this demo only; no download is needed. Custom Security settings may behave differently.

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

1. Prerequisites: hole ≥ 2.1.0, Docker, Python ≥ 3.10, git (node is not needed).
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

**Settings › Security** controls the optional guards described below. **Default** restores the nine
selected first-use guards/cards, **Plain hole** switches every guard/card off, and **Guarded** switches
all 64 on. Existing saved choices are preserved. Open a group, then an approval area only when needed;
search goes directly to a switch. Start checks and sandbox contents apply on the next start; tool limits,
unlocks and approval choices apply at once. Hole's own excludes, safe path validation and the prohibition
on committing over-mounted files remain in effect.

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
| Git panel | every changed file of the whole repository (files hidden from Claude marked with the eye and unchecked; **Open** / **Diff** / **History** on each row, a line number in a diff opens the file in Piper's editor), diffs, commit of the checked files, push after a confirm. A `core.hooksPath` inside the working tree (husky) is not run: the agent can edit it |
| Activity bar | Overview (badge: what needs you; always opens the whole queue), Agent, Host shells, then **Usage · Time · Audit · Stacks ↗ · Settings · Keys**. Stacks = the dev stack provider's own dashboard (when an extension supplies one) |
| Sessions list | compact Overview header with standalone `⌘K`, `+ Session` and collapse; always-visible search below. Bold title, project/issue and age; no source filters, last-message preview or workflow next-step labels. Ended starts folded; search also finds ended sessions. Permissions and errors remain visible |
| **Files** tab | the whole project (only `.git` never); files **hidden from Claude** (isolation policy / excludes) open, search and edit like any other, marked with the eye icon and a banner - sessions still never see them. The queue folds to the strip of session tiles (⌘B = the whole queue). **Tree**: scope All / Changed / Turn / Open, typing filters, git letters and change counts, tooling folders folded ("+ N system"; ⌘P / search leave the same folders out, "Search the excluded folders too" widens it), git letters also without a session, right click / ⌥N ⇧F6 F6 ⌫ = new, rename, move, delete (to Piper's trash, Undo) - also while the session works, except a file Claude edited in its current turn; a file hidden from Claude appears while a session runs only inside a directory its sandbox over-mounts (elsewhere the sandbox, started before it, would see it). **Editor** (CodeMirror 6): always editable, **also while the project's session works** (a save over Claude's newer edit is refused: Reload / Overwrite); ⌘S saves with the sha lock; imports folded, sticky class/method, change marks + Claude's last edit (purple) in the gutter, inline blame, IntelliJ problems as squiggles; when the session edits an open file it reloads and flashes the lines. One-line header: path › class › method and icons (@ = `path:line` into the prompt, ⌥⏎; ± diff ⌘⇧D; blame; history; outline; IntelliJ). **⌘P** searches files, #symbols, >actions, /text (empty = recent locations). Bottom panel (⌘⌥↓): Usages ⌥F7 · Search ⌘⇧F (git grep) · Problems · Diagnostics · Turns (this file's changes per turn); F4 / ⇧F4 step |
| ⌘P / ⌘⇧F | open a file by name (`Foo.java:120` jumps to the line; empty = recent files) / regex search in the project (package-manager caches like `.pnpm-store` skipped) |
| tree + preview | compact packages (`src/main/java` → `com.acme.shop`), dirs first, changed files marked; syntax highlighting (Java, Kotlin, TS/JS, XML/HTML, JSON, YAML, properties, SQL, shell, Python, CSS, Markdown; own lexer, no innerHTML) |
| click on `path:line` in the terminal | opens it in the **Files pane** next to the terminal (⌘ / ⇧ click opens IntelliJ); paths in the git panel, review, history and the conversation open the editor too |
| **Files pane** ⌘O | the file browser as a grid pane next to the terminal: tabs of open files (⌥W closes, middle-click too), the tree on demand (≡), drag the gap to resize. ⌘P from the terminal opens the file there; ⌘D adds a Database pane of the active project. Without the grid module a path opens the Files tab |
| In a file | ⌘F find (Aa / W / .*), ⌘L go to line, ⌘⇧O outline, ⌘+click (or ⌥+click) = go to definition (regex symbol index, ⌘[ back), ⌥F7 find usages, `#Name` in ⌘P = symbol search. Gutter marks changed lines against HEAD; **Diff** = side by side; **Note** adds a review note to the Git panel; **Preview** renders Markdown; images are previewed |
| **Edit** (⌘S) | small edits from the GUI: saved only if the file did not change since you opened it (sha) - also while a session works; eol and final newline kept; a file hidden from Claude is rewritten in place (its over-mount stays) with a crash journal, never when a running sandbox does not cover it; audited (`gui-write`, `hidden` for a file hidden from Claude). Create / rename / move never put a hidden file's content where a session sees it, and, while a sandbox of the tree runs, create a hidden path only inside a directory its sandbox over-mounts (its over-mounts were fixed and pinned at its start). A file over 2 MiB or a binary one says so (413 / 415) |
| **.http files** | IntelliJ's HTTP Client format, run **on the host** by JetBrains' `ijhttp` with the real `http-client.env.json` (+ `http-client.private.env.json`) - the sandbox never sees them. Environment picker (green dev / orange test / red prod, remembered per project), **▶ Run all**, ▶ next to each request (runs it alone with the globals the last run set, e.g. a `sessionId`), results below: status, time, `client.test` ✓/✗ with the failing line, headers (Authorization/Cookie masked, **show secrets**), the body formatted, earlier runs (yours and the sessions'). Production asks once more, and once more for requests that change data. **Share bodies with sessions** decides whether `http_last_result` gives a session the (scrubbed) bodies - on for dev/test, off for prod. No `ijhttp`: **Install** downloads it into `~/.piper/ijhttp` (needs Java 17+) |
| **HTTP** tab (session) | every `.http`/`.rest` file of the project in one list, grouped by directory, filterable by path, request name or URL, with how its last run ended (yours or a session's); opens the file run last; a file hidden from Claude is listed too, with the eye icon. For the picked file: environment, **▶ Run all on <env>**, the requests as rows (▶, method, name, URL resolved for the environment, status of the last run). A click (or Enter) unfolds a request: its last result (tests, headers, body), the request as written, **▶ Run** and **Open in Files**; double-click or `o` opens it in Files, `r` / ⌘Enter runs it, ↑↓ move. Production turns the run controls red and is never remembered (preselected only when it is the only environment); results of another environment are dimmed with "results are from …" |
| **Database** | activity bar **Database** or the session tab: the project's connections (dev from the project's dev stack when an extension provides one, test/prod through a tunnel Piper opens, class colour first), the object tree, table tabs (WHERE, sort, columns, FK jump ›, inline edit → pending changes **Submit ⌘S** / **Revert Esc**, + Row, ⌘⌫ delete, CSV import, copy as CSV/INSERT/JSON, DDL), SQL consoles (⌘↩ runs the statement at the caret, ⌘E explain, completion from the schema, **Read-write** = one open transaction until **Commit ⌘⇧↩** / Rollback), Claude's queries and the history on the right, **Connections** (add, password, tunnel, Test, reset pin, IDEA import). Raw data for you, no card; production changes ask once more in red. Your queries have their own timeout (Settings → Databases → Query timeout in the Database view, 300 s); a session's running read shows in its context panel's Database section with **Cancel**. Sessions get the same connections through `db_*` tools with cards |
| **Terminal (host)** | off by default: Settings → Terminal → **Host terminal (no sandbox)**. Then **⌥F12** (rail icon, session ⋯ → Host shell here, ⌘K → Host shell: <project>) opens login shells **on your Mac, without a sandbox**, in a project's folder - tabs, `⌥⇧T` new, `⌥⇧W` close, a red **HOST** strip above. Only the GUI's own token opens one (never `piper attach`, a session or MCP); open / close / exit are audited, never the content |
| **History** / **Graph** | commits of a file or the repository with their diffs (files hidden from Claude included), **Blame** with age colours, the branch graph |
| Context panel: **Last turn** (last section, folded by default) | what Claude changed in one turn: piperd snapshots the working tree at your prompt and at the turn end (temporary index + `write-tree`, no branch/index/ref change); files and diffs per turn, older turns in the picker, notes on lines as in the review diff |
| Git panel: CI / MR | pipeline status of the branch (GitLab through piperd; the session reads the failed job's log tail with `gitlab_job_log`), a notification when it turns failed; **Create MR…** after a push (title/description drafted from the commits, you confirm) |
| Inspector: **Diagnostics** | compiler/test errors (mvn, javac, tsc, eslint, gradle, python, junit) from the session's last Bash output, grouped by file, click = open in the Files pane, "Send to Claude" sends `path:line: message` |
| Inspector: **Stack** | the project's dev stack from the stack provider (an extension): services, logs (filter, auto-refresh), restart after a confirmation |
| **Usage** | tokens per day (30 days) and per project from the Piper sessions' transcripts |
| **Feed** (off by default: Settings → Feed) | Its actions are the queue's items, its events the queue's **Recently** and its chat the **Agent drawer** (⌘0, ⌘.). What it adds to the queue: **cards** = what waits for you (L5, permission prompt, confirmation with Allow/Deny, failed start, a session waiting for you (at once) with a reply box, failed CI of a project with a live session, new assignment/mention in GitLab with the comment as a quote, new production errors; each disappears when its reason does, **snooze** / **mute 1 h** (both undoable: **undo** / **cancel**), security items neither), **grey lines** = what happened (turn ended, session created/ended, handoff result, new comments, CI passed again, "handled …"; folded per key within 10 min, a "new" line, "Since 14:05: …"), **bubbles** = the chat with the Global agent. The input always writes to the Global agent: the message is queued and typed into its terminal when it idles at an empty prompt (the handoff rules, never into a permission prompt); the chat is its own transcript, so terminal and chat agree. **→ agent** on any item adds only a reference (`(ref: gitlab p/x#12)`), never the notification's text. Agent not running: **Start and send**. A sent message stays visible until it is in the agent's transcript; if the agent ended first it is "not delivered" with **Send again**. ✕ cancels a queued one. In the drawer Enter sends, Esc closes it. piperd derives everything on its own tick (`feed.py`), `feed/events-YYYY-MM.jsonl` keeps `retention_days` |
| **Keys** (activity bar, ? in the queue) | all shortcuts, grouped |
| **Settings** ⌘, (activity bar, palette) | **Security**: Recommended / Minimal / Strict profiles, applied in two clicks. Customize details keeps the original tree and directly visible approval switches, with bulk controls and search. An unset tree stays Minimal; no automatic Security onboarding. Other sections tune enabled guards and configure features, providers, projects and tools; inactive guard options link back to their switch. Each field says when it applies. Changes are audited, concurrent edits are refused (409), secrets stay masked, and only the GUI token may save. |
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
