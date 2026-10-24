# Piper desktop

An Electron window and tray for the independent Piper service. The desktop app never owns piperd,
ptyhost or a session: closing or updating the app leaves them running. The window loads the existing
web GUI unchanged, with isolated, sandboxed renderers and no Node integration.

First launch attaches to an answering local service, otherwise runs an existing `piper ensure`, otherwise
shows the setup wizard. Setup checks Docker/Podman without changing it, uses bundled Python, asks about
extensions, and runs `install.sh --yes --no-open --service` with an explicit extension decision. Docker is
the user's prerequisite; optional Podman installation is a separate last-resort action with the OS's
password prompt. Windows uses a WSL 2 distribution; only package installation explicitly runs as root.

The source of the layout and states is [Claude's Pen design](design/piper-desktop.pen), with
[PNG references](design/png/). Shared CSS controls and dark/light tokens are copied from the web GUI by
`build/prepare.mjs`; do not edit generated `src/ui/shared.css` or `src/ui/themes.json` by hand.

## Prepare and package

Node 22.14+ and npm are build tools only; end users do not need Node. Commands below use an absolute checkout
path. These commands never publish or install a service; macOS packaging signs the bundle ad-hoc
unless certificate credentials are supplied.

Use `ELECTRON_CACHE=/absolute/path/to/scratch/electron` and
`ELECTRON_BUILDER_CACHE=/absolute/path/to/scratch/builder` when the normal user cache is not writable.
The build config forwards the Electron cache explicitly to electron-builder.

```sh
PIPER_REPO=/absolute/path/to/Piper
npm --prefix "$PIPER_REPO/desktop" ci --cache /absolute/path/to/scratch/npm
node "$PIPER_REPO/desktop/build/prepare.mjs"
node "$PIPER_REPO/desktop/build/python.mjs" darwin arm64
PIPER_DESKTOP_ARCH=arm64 npm --prefix "$PIPER_REPO/desktop" run pack -- --mac --arm64 --publish never
```

Prepare resources after changing the installer, discovery module, product icons or web theme. It requires
the extension-discovery core (`piper/extfind.py` and the matching `extensions.py` with `API`). During integration,
`PIPER_DESKTOP_EXTFIND=/absolute/path/to/core/piper/extfind.py` selects the corresponding source read-only.
The build copies discovery and an API-only shim; it never imports extension code. CI requires the core to be
integrated in the checkout and deliberately fails when it is missing.

`build/python.json` pins python-build-standalone 3.13.17+20261021 and the SHA-256 of each archive. Windows
ships Linux Python for WSL, not Windows Python; its links (`bin/python3` → `python3.13`) are replaced by copies,
because Windows tar extracts them as Windows symlinks that 7-Zip cannot pack into the installer. Downloads go into ignored `vendor/<os>-<arch>/python`,
including Python's third-party licenses. Setup copies it to a stable app-data location, so an AppImage's
temporary mount never ends up in the service's PATH. `resources/` and `out/` are also ignored.
Runtime paths include the pinned Python version. Old runtimes are retained for services that still use
them; an app update never overwrites the interpreter of a running service.

Replace `darwin arm64` with `darwin x64`, `linux x64`, `linux arm64`, `win32 x64` or `win32 arm64`.
`PIPER_DESKTOP_ARCH=<arch>` limits the targets to that architecture; a build refuses to start when an
architecture it would package has no prepared Python (an installer without the runtime used to slip through).
`npm run dist -- --<arch> --publish never` creates the platform's configured installers: macOS DMG + ZIP,
Windows NSIS, Linux AppImage + deb. `npm run pack` only builds the unpacked directory.

macOS defaults to a complete ad-hoc bundle signature (`identity: '-'`, hardened runtime off), supported
by the pinned electron-builder 26.18.0. CI verifies the built bundle and the app copied from the released
DMG with `codesign --verify --deep --strict`. This is not an Apple Developer ID signature or notarization:
after the first launch attempt, allow it in System Settings > Privacy & Security > Open Anyway.
`build/config.cjs` uses certificate signing when credentials are explicitly provided
(`CSC_LINK` / `CSC_NAME`, `WIN_CSC_LINK`). Apple notarization additionally requires `APPLE_ID`,
`APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID`. Supply these through the build environment, never files in
the repository. Windows remains unsigned by default. Tag builds can opt into free SignPath Foundation
signing with repository variable `SIGNPATH_ORGANIZATION_ID`, variables `SIGNPATH_PROJECT_SLUG` and
`SIGNPATH_POLICY_SLUG`, and secret `SIGNPATH_API_TOKEN`. CI packages with `--dir`, signs the app, packages
with `--prepackaged`, signs NSIS, then refreshes updater hashes/blockmaps. Only this path sets
`publisherName` to `SignPath Foundation` for updater certificate verification. See the
[code signing policy and setup](../docs/CODE_SIGNING.md) for approval roles and artifact configurations.
Electron and bundled dependency licenses remain in the package, alongside LICENSE/NOTICE
and the complete Python license tree.

Updates use public `desktop-v*` releases of Kamest/Piper, separate from core releases. Unsigned macOS
offers a release-page download. Signed macOS, NSIS and AppImage use electron-updater and install on app
quit. Update channels include the architecture; draft releases are never offered. CI attaches installers
to a **draft** release only on a desktop tag. It never publishes the draft or changes repository visibility.

### Package manager manifests

For a published stable `desktop-vX.Y.Z` release, download its two DMGs and two Windows installers into
one directory, then generate checksummed manifests without installing dependencies:

```sh
node "$PIPER_REPO/desktop/build/manifests.mjs" desktop-v0.1.2 /absolute/path/to/assets /absolute/path/to/manifests
```

The output contains `Casks/piper.rb` for the personal `Kamest/homebrew-piper` tap and schema 1.10.0
multi-file winget manifests under `manifests/k/Kamest/Piper/<version>/`. Electron 45 requires macOS
13 Ventura or newer. `zap` uses `~/Library/Application Support/piper-desktop` and
`~/Library/Logs/piper-desktop`: Electron takes its name from the package metadata, while
`build.productName` names the bundle. The cask removes only desktop userData/log directories, not the
independent service or its sessions. Docker remains the user's own prerequisite.

After all six published-release smoke jobs pass, `desktop-release.yml` downloads the final assets,
generates and uploads `desktop-package-manifests`, runs `winget validate` on Windows (bootstrapping
WinGet if absent), and runs `brew style` and `brew audit --cask` in a temporary local tap on macOS.
Generation and validation require no distribution credentials. Publication steps are skipped without
their respective secrets:

- Create `Kamest/homebrew-piper` separately and set `HOMEBREW_TAP_TOKEN` with Contents write access
  to that repository to commit the validated cask there. Users can then run
  `brew install --cask kamest/piper/piper`. No tap repository is created by this workflow.
- Set `WINGET_TOKEN` to a GitHub classic token with `public_repo` scope for the submitting account.
  The pinned wingetcreate `v1.12.13.0` submits a pull request to `microsoft/winget-pkgs`; acceptance
  remains subject to its review. The token is passed by environment, never a file or command argument.

Do not enable either submission secret until the generated manifests and real release installers have
been reviewed. CI wiring is not evidence of a completed signing, store submission or downloaded update.

## Tests: OS sandbox only

Do not launch the app against a real Piper for tests. Every test imports the existing sandbox guard.
Run these commands **outside** another agent/OS sandbox:

```sh
PIPER_REPO=/absolute/path/to/Piper
node "$PIPER_REPO/desktop/test/run.mjs" unit
node "$PIPER_REPO/desktop/test/run.mjs" e2e
```

The host harness invokes `scripts/piper-sandbox.py`, then `node --test` with absolute test paths. It extracts
only named PNG artifacts from the sandbox's output into ignored `desktop/test/shots/`; tests cannot write
the checkout. The underlying unit command is:

```sh
python3 "$PIPER_REPO/scripts/piper-sandbox.py" -- node --test "$PIPER_REPO"/desktop/test/*.test.mjs
```

For a display-less Linux host, use `xvfb-run -a node "$PIPER_REPO/desktop/test/run.mjs" e2e`.
E2E requires the prepared resources and the host architecture's Python vendor download above; it exercises
the shipped Python in an isolated app-data directory, including when using a fake installer.
For the additional agent-browser snapshot and screenshot (install the CLI before entering the sandbox):

```sh
PIPER_E2E_AGENT_BROWSER=1 node "$PIPER_REPO/desktop/test/run.mjs" e2e
```

Unit tests cover CLI precedence, attach/ensure/wizard decisions, authentication and redaction (including
split output chunks), D1 row states, D2 preselection, single extension consent, D3 output, D5 checkpoints,
Podman/WSL commands, autostart formatting, tray counts/bitmap badge, updates and navigation. E2E launches
the real Electron with a fake HTTP service, fake CLI, fake installer and fake container commands in a temporary
home. It covers D1–D8, native-notification callback dispatch into the GUI handler, own-origin popouts,
single-instance focus, extension trust/untrust, offline state and service survival after app quit.
The notification test exercises the native Notification object's click listener; a physical OS notification
click and OS permission prompts still require manual validation.

`test/linux-docker.sh` is an opt-in Ubuntu 24.04/Xvfb image that runs sandboxed unit/E2E tests and builds
x64 AppImage + deb. It uses its own container and never mounts a host Docker socket or home directory:

```sh
sh "$PIPER_REPO/desktop/test/linux-docker.sh"
```

## Test-only switches

| Variable | Purpose |
| --- | --- |
| `PIPER_DESKTOP_HOME` | Isolated Electron userData and log directory; activates test safety checks |
| `PIPER_DESKTOP_CLI` | Exact fake CLI path; an absent override never falls back to a user's CLI |
| `PIPER_PORT` | Fake service port; test mode rejects 17895 |
| `PIPER_HOME` | Isolated backend state and tokens |
| `PIPER_DESKTOP_INSTALL_SH` | Fake installer; bundled Python is still used |
| `PIPER_DESKTOP_PLATFORM` | Platform decisions in pure modules; does not emulate OS APIs |
| `PIPER_DESKTOP_NO_CHROMIUM_SANDBOX=1` | Chromium-only flag needed inside the outer OS test sandbox; never the default |

Test mode requires all isolation paths, a fake CLI/installer and an alternate port. Start-at-login is
in-memory in test mode. E2E fixtures also set a temporary HOME, XDG_CONFIG_HOME, PROJECT_PATH and PATH.
`PIPER_E2E_AGENT_BROWSER=1` enables the additional browser-tool check; `AGENT_BROWSER` can select its executable.

## Implementation notes and verification handoff

- D1–D5 keep the four-step rail; D7 and D8 use the designed separate layouts and viewport sizes. Generic extension
  metadata has no `Adds` field, so D2 uses the neutral text “Features provided by this extension”. Missing Docker
  is described conditionally; no Docker Desktop installation is inferred from a failed `docker info`.
- Runtime output supplies installer phase hints. A failed installer reruns the same idempotent script, which
  skips already present components; a completed installer is not rerun when only service readiness failed.
- Setup log lives in Electron userData (`setup.log`), per the brief, rather than the illustrative Pen log path.
- WSL bootstrap uses UAC and a per-user RunOnce entry without rebooting the computer. After reboot it installs
  Ubuntu if necessary and rechecks prerequisites. A distribution's first-run account setup and availability of
  systemd/user services need clean Windows VM validation; account creation is not bypassed or run silently as root.
- Native tray chrome, window decorations and notification layout belong to the OS. The existing web GUI is
  not restyled. Compare identical states/viewports against `design/png/` before visual sign-off.

### Running under the test sandbox

- macOS Seatbelt: Chromium ignores `TMPDIR` for its singleton socket, so the fixtures set `MAC_CHROMIUM_TMPDIR`.
  The sandbox closes LaunchServices, so a window never gets OS focus (the single-instance test asserts a visible,
  restored window instead), and `nativeTheme` switching does not reach the renderer (the theme test emulates
  `prefers-color-scheme`).
- Linux bwrap: `/tmp` is an empty tmpfs; the harness sets `PIPER_SANDBOX_X11=1` so the sandbox binds the Xvfb
  socket and cookie read-only. In Docker, bwrap needs `--security-opt systempaths=unconfined` (Docker masks
  `/proc`); `linux-docker.sh` adds it.
- Electron downloads its binary in `postinstall`: a test sandbox has no network.
- Every `wsl.exe` call uses `--exec`: with `--` the default Linux shell re-parses the command line and eats the
  backslashes of Windows paths. Windows CI runs `test/wsl-smoke.cjs` against the runner's real WSL.
- `linux-docker.sh` writes screenshots to `$SHOTS` and installers to `$OUT` when those are set.
- Screenshots travel through the TAP stream, which node --test splits into several lines; `test/shots.mjs`
  carries a length and an end marker and the harness reassembles them (a damaged shot is reported, not saved).

Not verified by these tests: clean Windows/WSL bootstrap, reboot and login behavior, Linux desktop integration,
privilege prompts, real installation/migration, physical notification clicks, signing, notarization and
downloaded updates. Do not treat a successful unpacked build as runtime or visual sign-off.
