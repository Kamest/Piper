# Piper

A local workspace for running and supervising coding agents (Claude Code, Codex) in isolated
environments. Each session runs in its own [hole](https://github.com/lukashornych/hole) sandbox
rooted in one project; a Global agent plans the work and starts project sessions. Piper adds
approval cards, terminals that survive restarts, Git and database tools, HTTP request runs and
a web GUI on top.

**Status: first standalone snapshot.** Piper has been used daily on macOS; Linux and WSL are
supported but less exercised. There is no tagged release yet.

## Requirements

- macOS or Linux (WSL 2 works)
- [hole](https://github.com/lukashornych/hole) ≥ 2.1.0 and Docker
- python3 (standard library only) and git
- [Task](https://taskfile.dev) for the commands below (or call `sh scripts/piper.sh <command>` directly)

Node.js is needed only to rebuild the GUI and to run its tests; the built GUI is committed.

## Quick start

```sh
git clone https://github.com/Kamest/Piper.git piper
cd piper
task piper:doctor            # what this machine still needs; every line that is not ok names its fix
task piper:open              # starts piperd and opens the GUI
```

Projects are the git checkouts under `PROJECT_PATH` (your shell's value, else the directory
that holds this checkout). `task piper:install-service` runs piperd as a login service
(launchd / systemd `--user`); running sessions survive daemon restarts.

The full user guide is [piper/README.md](piper/README.md).

## Extensions

What is specific to one environment (a dev stack provider, default hosts, extra doctor checks,
protected paths, reserved environment names) lives outside the core in an extension: a
directory with an `extension.py`, listed in `PIPER_EXTENSIONS`. The hooks are documented in
[piper/extensions.py](piper/extensions.py). Without an extension Piper runs as is.

## Development

```sh
task piper:build             # rebuild piper/web/dist from piper/web/src (commit both)
task piper:test              # unit + GUI tests
task piper:e2e               # the whole gate (Docker and agent-browser suites skip when missing)
```

Every test runs inside the OS test sandbox of `scripts/piper-sandbox.py` (Seatbelt on macOS,
bwrap or a container on Linux); start the tests outside any agent sandbox. See
[CONTRIBUTING.md](CONTRIBUTING.md) and [ARCHITECTURE.md](ARCHITECTURE.md).

## License

[Apache License 2.0](LICENSE). Bundled third-party code keeps its own license, see [NOTICE](NOTICE).
