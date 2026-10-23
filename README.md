<p align="center">
  <img src="docs/media/banner.svg" alt="Piper: run a team of coding agents, approve what matters" width="100%">
</p>

<p align="center">
  <a href="https://github.com/Kamest/Piper/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/Kamest/Piper/ci.yml?branch=main&style=flat-square&label=ci&labelColor=26282c"></a>
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/github/license/Kamest/Piper?style=flat-square&color=26282c&labelColor=26282c"></a>
  <img alt="Platforms: macOS, Linux, WSL 2" src="https://img.shields.io/badge/platform-macOS%20%C2%B7%20Linux%20%C2%B7%20WSL%202-26282c?style=flat-square">
  <img alt="Backend: Python standard library only" src="https://img.shields.io/badge/backend-python3%20stdlib%20only-26282c?style=flat-square">
  <img alt="Agents: Claude Code and Codex" src="https://img.shields.io/badge/agents-Claude%20Code%20%C2%B7%20Codex-26282c?style=flat-square">
  <a href="https://github.com/Kamest/Piper/releases"><img alt="Latest release" src="https://img.shields.io/github/v/release/Kamest/Piper?include_prereleases&style=flat-square&color=ce9a2e&labelColor=26282c"></a>
</p>

<p align="center">
  <b>Piper is a local workspace for running several coding agents at once.</b><br>
  Every Claude Code or Codex session gets its own <a href="https://github.com/lukashornych/hole">hole</a> sandbox,
  rooted in one project.<br>
  The agents work on their own. When one wants to push, touch production or edit another project, you get a card.
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#what-you-get">Features</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="docs/GUIDE.md">User guide</a> ·
  <a href="https://kamest.github.io/Piper/">Project page</a> ·
  <a href="https://github.com/Kamest/Piper/discussions">Discussions</a>
</p>

<p align="center">
  <img src="docs/media/demo.gif" alt="A tour of Piper: the queue, the Global agent's plan, a conversation, two sessions side by side, Files, the Git window and Security" width="100%">
  <br><sub>A 90-second captioned walkthrough: <a href="docs/media/tour.mp4">tour.mp4</a></sub>
</p>

## Why Piper

One agent in one terminal is easy to watch. Five agents across four repositories are not: you lose track of
who is waiting for you, who is stuck and who is about to push. Piper keeps them in one place.

- **One queue.** Every session sorts itself into *Blocked*, *Your turn* or *Running*. The thing that needs you
  is always at the top.
- **Sandboxed by default.** Each session runs in its own container through hole. It sees its project, not your
  home directory, and never the files you excluded, such as `.env.local`.
- **You decide what needs you.** Choose *Minimal*, *Recommended* or *Strict*, or switch single rules. Anything
  outside the rules becomes a card with the exact command, a risk level and the rule that asked. What an
  agent does on its own still goes to the audit log.
- **Sessions outlive everything else.** Terminals live in a separate PTY host. Close the browser or restart the
  daemon and your agents keep working.

## What you get

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="docs/media/queue.png" alt="The queue next to a running session with a push approval card"><br>
      <b>Queue and approval cards.</b> Sessions grouped by what they need from you. A push waits on a card that
      shows the command and why it asked. You answer with Allow or Deny, or <kbd>y</kbd> / <kbd>n</kbd>.
    </td>
    <td width="50%" valign="top">
      <img src="docs/media/plan.png" alt="The Global agent's release plan across three projects"><br>
      <b>A Global agent that plans.</b> It reads your projects, writes the plan and starts one session per
      project, each in its own sandbox.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <img src="docs/media/grid.png" alt="Two agent terminals side by side"><br>
      <b>Side by side.</b> Put sessions, host shells or database panes in a grid. Every terminal is the
      agent's real TUI.
    </td>
    <td valign="top">
      <img src="docs/media/conversation.png" alt="A finished turn read as a conversation"><br>
      <b>Read it as a conversation.</b> A transcript view with tool calls, failures and token usage. You can
      filter it and search across sessions.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <img src="docs/media/git.png" alt="The Git window with branches, worktrees, history and a side-by-side diff"><br>
      <b>Git built in.</b> Branches, worktrees, history and side-by-side diffs. Review what an agent changed,
      then commit and push it.
    </td>
    <td valign="top">
      <img src="docs/media/files.png" alt="The Files editor with the agent's new component open"><br>
      <b>Files.</b> Browse and edit the project while the agent works. Hidden files are marked, and Piper asks
      before you overwrite a file the agent has just changed.
    </td>
  </tr>
</table>

Also included: **Settings › Security**, where every guard has its own switch
([screenshot](docs/media/security.png)). There is also a database client with SSH tunnels, `.http` request
runs, handing a session from Codex to Claude and back, Claude usage limits, remote access over your tailnet,
and extensions for anything specific to your environment.

<sub>The screenshots and the GIF show demo data generated by <a href="docs/media/showcase.py">docs/media/showcase.py</a>
on top of the end-to-end test harness. No real project or account is involved. See <a href="docs/media/README.md">docs/media</a>.</sub>

## How it works

```mermaid
flowchart LR
  B["Browser GUI"] -- "HTTP + token, 127.0.0.1" --> D["piperd<br/>Python stdlib"]
  D <--> P["ptyhost<br/>owns the terminals"]
  P --> SB
  subgraph SB["hole sandboxes"]
    direction TB
    S1["Claude Code<br/>project A"]
    S2["Codex<br/>project B"]
    G["Global agent<br/>no project mounted"]
  end
  SB -. "MCP tools: push, start session, DB, HTTP" .-> D
```

- **piperd** (`piper/piperd.py`) is a local daemon that uses only the Python standard library. It serves the GUI,
  keeps session state in `~/.piper`, and answers the agents' MCP tool calls. When a security rule holds a call,
  piperd turns it into a card.
- **ptyhost** (`piper/ptyhost.py`) runs on its own and owns every terminal, so piperd can restart while sessions
  keep running.
- **hole** starts each session's container with only its project mounted, minus the excluded paths.
- **The GUI** is TypeScript compiled to `piper/web/dist`, which is committed. You need Node.js only to change it.

More in [ARCHITECTURE.md](ARCHITECTURE.md).

## Quick start

Start Docker, then:

```sh
curl -fsSL https://kamest.github.io/Piper/install.sh | sh
```

[install.sh](install.sh) checks git, Python and Docker, and installs hole and Task after asking. It clones Piper
into `$PROJECT_PATH/piper`, or `~/.local/share/piper` when `PROJECT_PATH` is not set. If you have no
`~/.hole/settings.json`, it copies the example there. Then it runs the doctor and opens the GUI. It never uses
`sudo` and never overwrites your files. `sh install.sh --help` lists the options.

<details>
<summary><b>What it needs, and the same by hand</b></summary>

**What you need:**

- macOS or Linux (WSL 2 works);
- Docker or Podman with Compose: Docker Desktop, OrbStack, Colima or Rancher Desktop on macOS, Docker Engine 26+
  on Linux;
- [hole](https://github.com/lukashornych/hole) ≥ 2.1.0:
  `curl -fsSL https://raw.githubusercontent.com/lukashornych/hole/main/install.sh | bash`;
- Python ≥ 3.10 and `git` (standard library only, nothing to `pip install`);
- [Task](https://taskfile.dev) (`brew install go-task`), or call `sh scripts/piper.sh <command>` directly;
- a Claude or ChatGPT account for Claude Code or Codex.

**Step by step:**

```sh
git clone https://github.com/Kamest/Piper.git piper
cd piper
task piper:doctor            # what this machine still needs; every line that is not ok names its fix
mkdir -p ~/.hole && cp -n examples/hole-settings.json ~/.hole/settings.json   # excludes + registries, edit later
task piper:open              # starts piperd and opens the GUI
```

</details>

Then press **+ Session**, pick a project, the agent and the task. The first start builds the sandbox image,
which takes minutes once. Each agent logs in once: `/login` in Claude Code, *Sign in with Device Code* in Codex.

**Why hole and not plain Docker?** hole gives the agent no route out except a gateway that denies what you did
not allow, hides excluded files inside the container and destroys it on exit. Piper adds the queue across
sessions, the cards and optional guards on top. More in the [user guide](docs/GUIDE.md#faq).

Projects are the git checkouts under `PROJECT_PATH` (your shell's value, otherwise the directory that holds this
checkout). `task piper:install-service` runs piperd as a login service (launchd or systemd `--user`).

The [user guide](docs/GUIDE.md) walks through the first session and daily use. The complete reference is
[piper/README.md](piper/README.md), and the optional services (GitLab, GitHub, Mattermost, IntelliJ IDEA,
production checks) are in [Integrations](piper/INTEGRATIONS.md).

## Status

**Early release.** Piper is used daily on macOS and Linux; WSL 2 is supported but less exercised. Expect rough
edges and settings that still change between releases. What is known is in the pinned issue
[Known rough edges](https://github.com/Kamest/Piper/issues/3), changes are in the [CHANGELOG](CHANGELOG.md).
Ask in [Discussions](https://github.com/Kamest/Piper/discussions) and
[report what breaks](https://github.com/Kamest/Piper/issues).

## Extensions

Anything specific to one environment lives outside the core, in an extension: a directory with an
`extension.py`, listed in `PIPER_EXTENSIONS`. An extension can add a dev stack provider, default hosts, extra
doctor checks, protected paths or reserved environment names. The hooks are documented in
[piper/extensions.py](piper/extensions.py). Without an extension, Piper runs as it is.

## Development

```sh
task piper:build             # rebuild piper/web/dist from piper/web/src (commit both)
task piper:test              # unit + GUI tests
task piper:e2e               # the whole gate (Docker and agent-browser suites skip when missing)
```

Every test runs inside the OS test sandbox of `scripts/piper-sandbox.py`: Seatbelt on macOS, bwrap or a
container on Linux. Start the tests outside any agent sandbox. See [CONTRIBUTING.md](CONTRIBUTING.md),
[SECURITY.md](SECURITY.md), [ARCHITECTURE.md](ARCHITECTURE.md) and the [code of conduct](CODE_OF_CONDUCT.md).

## License

[Apache License 2.0](LICENSE). Bundled third-party code keeps its own license, see [NOTICE](NOTICE).
