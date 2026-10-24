# Piper user guide

From an empty machine to your first supervised agent, then daily use. The complete reference, every setting
included, is [piper/README.md](../piper/README.md). GitLab, GitHub, Mattermost, IntelliJ IDEA, the morning
production check, Time and remote access are in [Integrations](../piper/INTEGRATIONS.md).

- [What you need](#what-you-need)
- [Install](#install)
- [The first session](#the-first-session)
- [Daily use](#daily-use)
- [Security presets](#security-presets)
- [Update and uninstall](#update-and-uninstall)
- [FAQ](#faq)

## What you need

| | Why | Install |
| --- | --- | --- |
| macOS or Linux | Piper runs on the host. WSL 2 works, but it is used less than macOS and Linux | |
| Docker (or Podman) with Compose | hole runs every session in containers | Docker Desktop, OrbStack, Colima or Rancher Desktop on macOS; Docker Engine 26+ with Compose 2.26+ on Linux |
| [hole](https://github.com/lukashornych/hole) ≥ 2.1.0 (2.4+ recommended) | the sandbox: one container per session, a network gateway that denies what you did not allow, excluded files hidden | `curl -fsSL https://raw.githubusercontent.com/lukashornych/hole/main/install.sh \| bash` (installs to `~/.local/bin`) |
| Python ≥ 3.10 and git | piperd uses only the standard library, so nothing to `pip install` | usually present; `brew install python git` |
| [Task](https://taskfile.dev) (optional) | the `task piper:*` shortcuts | `brew install go-task`, or call `sh scripts/piper.sh <command>` instead |
| A Claude or ChatGPT account | each session logs in to Claude Code or Codex once | |

Node.js is needed only to change the GUI, never to run it.

## Install

The short way: start Docker, then run

```sh
curl -fsSL https://kamest.github.io/Piper/install.sh | sh
```

It does the steps below for you. It asks before it installs hole (with hole's own installer) or Task (with
Homebrew). It clones Piper into `$PROJECT_PATH/piper`, or `~/.local/share/piper` when `PROJECT_PATH` is not
set, and never overwrites your files. Add `--yes` to skip the questions, `--dir DIR` to choose the folder and
`--service` to start piperd at login.

Installation also finds registered extensions and nearby `piper-extension.json` manifests. Before loading
an extension's wrapper it shows its label, path, remote and commit and asks once. `--yes` accepts registered
extensions; scan results need a terminal answer or `--extension <path>`. Without a terminal and explicit
consent it prints the command for later. `--no-extensions` runs the core alone. List or revoke consent with
`piper extensions [--json]` and `piper extensions untrust <path>`; `piper extensions trust <path>` grants it.

The manual way:

```sh
git clone https://github.com/Kamest/Piper.git piper
cd piper
task piper:doctor            # every line that is not ok names its fix
```

Without Task, every `task piper:<command>` in this guide is `sh scripts/piper.sh <command>`.

Doctor checks hole, Docker, the Docker VM type, git and Python. One warning almost everyone sees on a new
machine is **No ~/.hole/settings.json**. Sessions still start, but with hole's defaults: no excludes, no
libraries, no network rules of your own. Start from the example and edit it for your stack:

```sh
mkdir -p ~/.hole && cp examples/hole-settings.json ~/.hole/settings.json
```

It hides `.env` files, keys and `node_modules`, and allows the usual package registries. A project can add its
own rules in `<project>/.hole/settings.json`; with *Review .hole/settings.json changes* switched on (Settings ›
Security), Piper shows you the diff before it accepts a changed one. The
keys are in [hole's configuration reference](https://github.com/lukashornych/hole#configuration).

Then:

```sh
task piper:open              # starts piperd and opens the GUI
task piper:install-service   # optional: piperd starts at login (launchd or systemd --user)
```

Projects are the git checkouts under `PROJECT_PATH`: your shell's value, otherwise the directory that holds the
Piper checkout. **Settings → Projects** adds others, or a glob such as `~/development/*`.

## The first session

1. A new install shows the **setup card**: the doctor's checks, and which parts of Piper you want to see
   (*Basic*, *Developer* or *Everything*; Settings → Features changes it later).
2. Press **+ Session**, pick a project and the agent (Claude Code or Codex), and write the task. To try it
   first without a project of your own, run `piper demo`: it creates a small demo project and starts a session
   that ends on an approval card.
3. **The first start builds the sandbox image.** That takes minutes, once. Later starts take seconds.
4. When the project has its own `.hole/settings.json` that asks for more than the sandbox gives, hole asks
   whether you trust it. Answer in the terminal; Piper never answers it for you.
5. Log in once. In Claude Code, type `/login`. In Codex, choose *Sign in with Device Code*, because the browser
   callback cannot reach the sandbox. Under the first-use preset *Minimal*, sessions use your own `~/.claude`
   and `~/.codex`, so an existing login just works. Switch on *Own ~/.claude per session* and *Own ~/.codex per
   session* in Settings › Security to keep them out of the sandbox; the login is then kept in Piper's template.

From then on, the session works on its own. When it wants something outside the rules, it appears under
**Blocked** in the queue with a card.

## Daily use

- **The queue** sorts every session into *Blocked*, *Your turn* and *Running*. What needs you is on top.
  **⌘J** opens the next item that needs you.
- **Cards** show the exact command, a risk level and the rule that asked. **y** allows, **n** denies.
  **Allow for this session** remembers the choice for that session only.
- **Terminal and Conversation:** every session is the agent's real terminal. The Conversation tab reads the same
  transcript as text, with tool calls and token usage.
- **The Global agent** (⌘0) mounts no project. It reads your checkouts, writes a plan and proposes one session
  per project; you open each proposal from its card.
- **Grid** (⌘G): up to nine panes of sessions, host shells and database panes.
- **Files, Git, Database, HTTP:** browse and edit the project while the agent works, review its diff, commit and
  push, query a database through an SSH tunnel, run `.http` requests. Piper asks before you overwrite a file the
  agent has just changed.
- **⌘K** finds everything else; **Keys** in the activity bar lists every shortcut.
- **Your own terminal:** `piper attach` opens a live session in Ghostty, iTerm or Terminal, and the session stays
  in the GUI too. **Ctrl-]** detaches.

## Security presets

**Settings › Security** has three presets. Each guard is also its own switch, and everything an agent does on
its own still goes to the audit log.

| Preset | The agent asks you before |
| --- | --- |
| **Minimal** | production data, writes to test databases, HTTP to test, production or unknown hosts, allowing a new network domain, edits to other projects' hooks and build files, changing Piper's settings |
| **Recommended** | everything in Minimal, plus pushing, git outside the sandbox, merging and running CI jobs, editing other projects, stopping a session |
| **Strict** | every action that has a switch |

Hole's own guarantees do not depend on the preset: excluded files stay hidden, the network gateway allows only
what your settings allow, and nothing outside the project and its libraries is mounted.

## Desktop app

The optional desktop app opens the same Piper GUI in its own window and adds a tray menu. Download it for
macOS, Windows or Linux from the [desktop-v0.1.3 release](https://github.com/Kamest/Piper/releases/tag/desktop-v0.1.3)
(the [README](../README.md#desktop-app) lists the files), or on macOS with
`brew install --cask kamest/piper/piper`. If Piper is running, it connects; if a `piper` command exists, it asks that command to ensure the service
is running. Otherwise the first-run wizard checks the prerequisites, shows any discovered extension and asks
before installing. Docker or Podman must be running. Installing Podman is an optional last-resort action;
Windows also needs WSL 2 and a Linux distribution, and enabling WSL may require a restart.

The tray shows what is waiting, opens Piper in the window or browser, manages extensions, checks for desktop
updates and controls “Start at login”. Loading or turning off an extension restarts the Piper service while
sessions keep running. Closing the window hides it; “Quit Piper window” quits only the desktop app. If the
service becomes unavailable, the window retries every five seconds and offers “Start Piper”. On unsigned
macOS builds, updates open the release download page. The builds are not signed yet: on macOS, after the first
launch is blocked, press Open Anyway in System Settings › Privacy & Security; on Windows, choose More info › Run anyway. Build commands and verification
status are in the [desktop README](../desktop/README.md).

## Update and uninstall

```sh
git pull
task piper:restart           # a new piperd; running sessions survive
```

When a release changes ptyhost (the [CHANGELOG](../CHANGELOG.md) says so), run `task piper:stop -- all` and
`task piper:open` once. That ends every session; **Continue** resumes them.

To uninstall: `task piper:uninstall-service`, `task piper:stop -- all`, then delete the checkout and `~/.piper`
(sessions, settings, the audit log, Piper's login templates).

## FAQ

**Why hole and not plain Docker?** A sandbox only helps when the agent cannot widen it. hole puts the agent in a
container with no route out except a filtering gateway that denies what you did not allow, on every protocol and
port. It also hides excluded files inside the container and destroys the container on exit. Piper adds what one
sandbox per terminal does not give you: the queue across sessions, cards for the agent's requests outside the
rules, and optional guards in Settings › Security, such as a scan before every start for sensitive files that
nothing would hide and a check inside the running container that the excluded files are really hidden.

**Which agents?** Claude Code and Codex. hole can also run Gemini CLI and custom agents, but Piper does not
support them yet.

**What does it cost?** Piper is free (Apache 2.0) and runs locally. The agents use your own Claude or ChatGPT
plan or API keys.

**Does anything leave my machine?** piperd listens on `127.0.0.1` only, with a token. The sessions reach what
your hole settings allow, and the integrations you turn on talk to their own services. Nothing else.

**Is it ready?** It is an early release that is used daily on macOS and Linux. What is known to be rough is in
the pinned issue [Known rough edges](https://github.com/Kamest/Piper/issues/3). Questions go to
[Discussions](https://github.com/Kamest/Piper/discussions).
