# Changelog

Notable changes per release. A release whose notes say **ptyhost changed** needs `task piper:stop -- all` and
`task piper:open` once, which ends every running session. Every other update only needs `task piper:restart`, and
running sessions survive it.

## Unreleased

- The migration news includes Workspaces and starting sessions directly on your own server. Moving
  a running session to a server appears separately in Also new with an EXPERIMENTAL tag. The header and
  dismissal stay visible while the highlights scroll, including on narrow screens and with larger
  interface text. The existing one-time display rules are preserved.
- Desktop CI retrieves the same pinned official build dependencies through the GitHub release API and
  seeds the supported Electron and installer-tool caches when public download URLs time out. SHA-256
  checks remain mandatory; no runtime versions or application behavior change. Python download errors
  fall back to the official API only for server/transport failures, preserving authorization failures.

- Waiting detection retains unanswered Codex questions across unrelated user messages, matches
  structured replies to the tool call and item, and preserves partial answers after replay. Explicit
  cancellation closes only its matching question. Native transcripts are never rewritten.
- An idle reminder keeps the same waiting turn; informational authentication/agent-completion hooks
  do not create a permission request. Desktop/mobile predictors suppress legacy idle duplicates while
  genuine permission prompts still notify. Desktop inline Reply requires confirmed prompt readiness.
- A bounded own-conversation lifecycle index recovers a lost Stop only from a completed turn after
  process start, with no newer hook, child/background job, approval card or authentication blocker.
  Recovered completion keeps automatic input locked until a genuine prompt hook arrives. Meaningful
  detector changes are journaled with safe source/time metadata. Regressions: `test_waiting.py`,
  `test_log_recovery.py`, `test_sessions.py`, desktop `notify.test.mjs`, GUI `--log-recovery-only`.

- Server setup installs missing Claude Code and Codex before agent sign-in, without sudo or shell
  installers. Agents use the vendor's latest release, verified over HTTPS against Claude's vendor
  manifest or GitHub release digest and package checksums (agents self-update); Python stays pinned.
  Downloads stream to private temporary files with size limits; verified packages and command links
  are published atomically. Missing Python and Windows SSH shells now report actionable prerequisites
  (Windows servers require WSL2 with systemd). Offline regressions cover installation safety, layouts,
  durable writes, PATH and SSH diagnostics (test_remote_service.py, test_server_setup.py, test_remotes.py).
- Fix: on Linux, Set up server enables lingering for the SSH account (`loginctl enable-linger`, no sudo);
  without it systemd stopped the Piper service and phone backup when the last SSH login ended. A refusal
  asks for the one `sudo loginctl enable-linger` command (test_server_setup.py, test_remote_service.py).
- Fix: a refused server write (a clone the forge refused, a blocked agent download) no longer marks a running
  setup pass halted while its later steps still run; the GUI stopped following the pass and stayed on
  "Checking server…" (test_server_setup.py).
- Contributor instructions keep one current task record with all requirements, clarifications, open
  items and evidence. Handoffs preserve source references and earlier details so a shorter summary
  cannot silently discard unfinished work; implemented, tested and deployed remain separate states.
- Direct sessions keep their language instructions without Hole-only masked-file hints or nested-agent
  full-access permissions, even when those options are enabled for Hole sessions. Regression coverage
  checks the actual Claude/Codex Direct launch prompts and the opt-in Hole instruction matrix.
- The `propose_session` tool returns the created session's effective agent, runtime, project and worktree
  alongside its id and state, so the caller can verify the requested launch context without guessing.
  Existing fields and approval behavior are preserved; session and worktree regressions cover the reply.
  Both changes verified with the sandboxed `task piper:test` gate and companion compatibility against
  the edited core checkout; actual agent startup and native/device behavior remain separate checks.
- Server setup supports macOS over SSH without sudo: selects an available Python 3.10+ (including
  ~/.local/bin), or installs pinned, checksummed standalone CPython 3.12.16 (20261026) in the SSH user's
  home on macOS arm64/x86_64 and glibc Linux aarch64/x86_64. Step 1 records Python installation as a durable
  write, reports progress, and continues with the selected interpreter; lost answers require reconciliation.
  The SSH peer travels compressed, preserving stdin and terminal arguments within the command size limit.
  Setup installs server and phone backup LaunchAgents in the launchd user domain, and updates
  their extension environment. Hole is reported as unavailable on macOS; sessions run directly.
  The LaunchAgents use the Background session type (launchd refuses a default Aqua agent in the user
  domain), and ~/.local/bin comes first in PATH on macOS so its /usr/bin stubs (java) do not shadow the
  installed tools. Verified on a real macOS arm64 server over SSH: Python installed by setup, service running
  under launchd, extension tools installed (test_remotes.py, test_server_setup.py, test_remote_service.py).
- Fix: a project clone refused by the forge (server key not added yet) showed "The server answer was lost";
  a failed clone that left nothing behind is now a definite refusal with its reason (test_server_setup.py).
- Git access names only the forges that still refuse the server key ("Add this public key to your gitlab.com
  account…"); a forge that already accepts it is no longer listed (test_server_setup.py).
- Fix: Server setup on a server without Python 3.10+ or systemd said "The server answer was lost" instead of
  the reason. The service installer now refuses such a server before writing anything, so step 1 shows
  what to install or that SSH mode is needed (test_remotes.py, test_server_setup.py).
- Server setup shows a Log under the steps: each step state with its reason, newest first, open while
  running or after a failure. It keeps only the user-facing step texts, never command output or exception
  values (test_server_setup.py, serverSetup.test.mjs).
- Move to Server: a halted move that created nothing on the server can be cancelled with Cancel move; the
  session is an ordinary local session again and its work stays as it is (`POST .../move/cancel`,
  test_transfer.py, move.test.mjs).
- Fix: Move to Server stopped with "pathspec … did not match any files" when the session had a deletion
  already staged; the WIP commit now takes staged deletions (test_gitops.py).
- Desktop build: `minimatch` in electron-builder's dependencies is overridden to 11.0.5
  (GHSA-g3hv-75wg-m249, denial of service via glob patterns). Build-time only; no upstream
  electron-builder release ships the fixed version yet. Other `minimatch` majors are unaffected.
- Fix: a turn whose start snapshot had failed no longer stops the daemon's startup upkeep with a
  TypeError; the other turns and sessions are pinned and pruned again (test_turnreview.py).
- Fix: Claude usage limits no longer go blank with "login expired" on macOS while Claude Code is signed in.
  Claude Code there refreshes its login only in the login keychain; Piper read just the file copy, which
  expires within hours. The limits now also read the keychain item (read-only, never refreshed) and use
  the login that expires last (test_limits.py).

- Fix: inline approval cards keep a bounded width and reachable decision buttons when facts are long,
  including narrow screens and larger interface text. Evidence keeps every original row, with denser
  spacing; SQL, diff and preview overflow remain separate (approval-card browser tests).

## 0.1.8 (2026-11-08)

- Fix: terminal reflow keeps standalone issue and merge-request references separate from a preceding
  wrapped URL or path. Regression tests cover full-width boundaries, qualified references and actual
  terminal-to-Files-to-issue navigation; ordinary URL fragment and query wrapping still work.

## 0.1.7 (2026-11-08)

- Fix: file paths and URLs hard-wrapped by a terminal CLI remain one clickable target, including
  breaks after a hyphen or slash and full-width mid-token breaks. Link ranges keep their displayed
  cell positions; ordinary paragraphs and separate references are not joined. The complete URL
  takes priority over a valid shortened first-line prefix (references, term-resize and browser tests).
- Fix: an explicitly named sibling project can open its file in Files after the exact path in the
  emitting project was checked. Only projects on the same host are considered; ambiguous prefixes
  offer a choice instead of selecting an arbitrary project (cross-project-reference tests).

## 0.1.6 and Desktop app 0.1.15 (2026-11-07)

- Server setup › Phone backup says how many phones the server is paired with instead of still asking to pair
  after pairing; the peer reports only the count, never device ids (test_server_setup.py).
- Server setup › Configuration no longer fails with "Configuration manifest exceeds the setup limit" when a
  full `development/` mirror plus server-only files there pass 10000 entries; the peer inspects up to 50000
  while writes stay bounded (test_server_config_sync.py).
- Server setup › Secrets also offers real `.env*` files of mirrored projects without a template (`.env.local`,
  `.env.sentry-build-plugin`; `.env.local.example` describes `.env.local`), by key name, for Copy to Server.
  Files under `development/`, `build/`, `dist/` and dependencies are skipped. The server peer now writes
  `.env.*` files as key=value lines (needs Update server). Extensions may name where this computer keeps a
  secret in an XML file (`local`, gated by `extensions.SERVER_SECRET_LOCAL`); its value is copied only when
  all named places agree, e.g. Maven settings. Values never reach responses or setup state
  (test_server_config_sync.py, test_server_setup.py, serverSetup.test.mjs).
- Server setup › Secrets offers Copy to Server for missing values found in the same `.env` on this computer.
  Piper reads and sends them over SSH itself; the page gets only whether a value exists. Other values stay
  typed (test_server_config_sync.py, serverSetup.test.mjs).
- Server setup › Secrets no longer waits for keys from a project's `.env.example` when that `.env` is absent on
  this computer too: the server mirrors the computer, the rows stay visible as optional
  (test_server_config_sync.py).
- Fix: the configuration review no longer reads development/ files that no longer fit the setup limit
  (about 3x faster on a large folder), and its button says Comparing… while it prepares. Review and copy wait
  up to 5 minutes instead of failing after 15 s with only a brief notice
  (test_server_config_sync.py, serverSetup.test.mjs).
- Fix: a paired phone can add a backup computer or server: Settings › Mobile access › Scan backup code opens
  the in-app scanner and leads to the Pair as backup confirmation. Before, the only path was the Camera app,
  which opened a browser and started a new pairing. Pair phone on the server explains the steps
  (mobileboot, mobileconnection, serverSetup tests).
- Fix: server setup copies only text files up to 1 MB from each project's `development/` folder and skips
  build output, so a large scratch folder no longer fails the Configuration step; the review lists one row
  with how many files were left out. Machine configuration and policy are packed first. A failed step shows
  its reason instead of a generic line (test_server_config_sync.py, test_server_setup.py).
- Fix: an updated server service no longer fails to start on a policy written by an older Piper.
  The security migration drops items removed since (such as a retired sandbox guard) instead of
  refusing the whole file; the audit records which. Server setup now names the transport reason of a
  failed step and logs every failed or lost step to piperd.log (scrubbed), and a lost server answer
  puts Check result / Retry in the panel header and the server list instead of a disabled Update
  server. Verified: regression tests fail without the fixes; Python and node suites pass.
- Fix: server setup mirrors projects kept outside the local projects root (another root such as
  `~/development`) under their project key, and a server whose projects folder lies outside the SSH home
  (`/srv/projects`) can check, clone and fill project secrets there once the running service verifies that
  folder; an unverified folder is refused before any write (test_server_setup.py, test_server_config_sync.py).
- Fix: server setup no longer stalls on common servers (test_server_setup.py, test_server_config_sync.py):
  the automatic project choice leaves out projects outside the local projects root instead of failing Git
  and Projects; the service update waits for the restarted service to answer before verifying it; phone
  backup builds its runtime with the account's own pip where the distribution lacks `python3-venv`; and
  configuration can mirror `development/` into a verified service projects folder outside the SSH home.
- Fix: the Settings › Servers row follows a running server setup and shows its halted result instead
  of staying on "Updating server…" (serverSetup.test.mjs).
- Fix: server setup commands reach the account's systemd user manager (XDG_RUNTIME_DIR and its
  bus socket path are passed; credentials still are not), so syncing extensions and the phone
  bridge can restart their user services instead of failing with "Failed to connect to bus".

- Human-selected external folders can be browsed read-only without registering a project or granting
  an agent access. Images use a bounded preview; other artifacts offer download.
  Navigation stays within the selected folder and preserves runtime/extension protections.

- New session can clone a repository or create an empty project without losing its task draft.
  Its project list expands inside the dialog scroll area, leaving the action footer reachable.
  Long paths wrap inside the picker; keyboard selection survives list scrolling under a resting pointer.
  Location offers a native parent-folder picker or a folder picker on the selected Piper server.
  Creation runs as a cancellable background job, refuses nonempty destinations and credential-bearing
  URLs, and registers the project before returning to the launcher. URL validation is bounded and
  checks transport components without backtracking. No session starts automatically.
  Running creation keeps its Cancel action keyboard-accessible and ignores Escape even after focus
  leaves a disabled control. Discard confirmation hints disappear with their dialog or a revised draft,
  preserving unrelated alerts and keeping the next dialog's controls reachable.

- Git windows recognize staged and unstaged moves, show the original path and stage both sides of a
  move together. Rename discovery uses a temporary index without changing the project's index.
  Terminal file/URL links span soft-wrapped lines; issue links use their explicit or linked repository,
  with a repository chooser when the session has several possible sources.

- Optional Features → Stack overview opens all running provider stacks and substacks from the activity
  bar. Dev stacks uses the shared All tools pin preference and sits immediately above Settings when
  pinned; an unpinned overview remains reachable from All tools. Stop all previews its targets, keeps
  data and reports partial failures with a retry action.
  Providers without the optional inventory contract display an explicit unsupported state.

- Interface settings include independent interface, editor and terminal font sizes, unsaved previews
  and reset. Saved changes update open clients, measure their editors and refit their terminals;
  previews explicitly distinguish local drafts from Save. Settings keep hidden switch inputs inside
  their labels so keyboard navigation preserves the fixed header; Reset leaves Save reachable on phones.
  Ended sessions can be set aside for Later; opening one shows its
  Conversation without restarting the agent, including when another conversation is already visible.

- Native workspace bars keep their fixed height above restart notices so system controls never cover the notice
  text or its Restart action. Workspace tabs retain their non-draggable click targets.

- Workspaces switch in the window bar without reloading mounted terminals. Renderer recovery is scoped
  to the affected window instead of marking the whole app offline. Failed top-level loads verify
  service reachability before declaring an outage; subframe failures leave other workspaces intact. Sidebar rows and headers share the
  stored session name, with the project shown separately in the header.
  Host pane headers retain a readable path with long session names, and the Agent drawer starts
  below the workspace bar and update notice so its actions stay reachable.
  Compact Host titles share a row with their owner even in a nine-pane grid with a waiting approval.
  On phones, the covered detail leaves keyboard navigation until the drawer closes or the window
  widens; closing a dialog restores focus to an accessible control.
  Notifications follow the measured top of inline approvals, preserving access to decision controls
  and scrolling long stacks; resize updates are batched into animation frames.

- Files offers local file-manager reveal and prepared native drag-out actions. Worktree files use the
  authoritative checkout root even when the project registry contains only the base checkout.
  Windows x64 maps validated paths through the selected WSL distribution; remote and Windows ARM
  paths retain the unsupported native-action/text-drag fallback. Default-application launch is unavailable:
  document handlers can execute files even without an executable bit, and checked paths can change.
  Explicit paths to another registered
  project open that project's Files with a return action; other files open in a bounded read-only tab.
  These GUI-only reads preserve runtime-state protections and never expand an agent's file access.
  Protected roots are also checked by filesystem identity, including case and filesystem aliases.
  Sandboxed Python, client and desktop suites, real HTTP/PTY fixtures and headless browser checks cover
  these changes. Native macOS file actions/window behavior and live provider authentication remain
  unexercised; native file callbacks are verified with mocks. Native Electron window ownership,
  renderer recovery and service reconnect are exercised on an isolated Linux/Xvfb display.
  Real XTest drag-out also transfers a linked-worktree file into a separate process, checking its
  absolute path and exact bytes on Linux x64 and ARM. Windows x64 verifies selected-distribution WSL
  conversion and reads the Linux fixture's exact bytes from Windows; Windows native drop remains unexercised.


- Server setup GUI (S3): Settings > Servers presents the ten-step durable setup snapshot,
  explicit setup/update, per-step fixes and unknown-outcome checks/retries. Running setup is
  polled every two seconds only in the visible detail panel; service servers get a collapsed
  readiness summary, while SSH-only and limited mobile keep their existing scope. Configuration
  review shows additions, changes, removals, server edits and exclusions before confirmed apply;
  refused plans require a fresh review. Project selection, public-key registration guidance and
  agent sign-in reuse the existing APIs and SSH terminal. Secret inputs are cleared on submission
  and dismissal, and never stored or echoed in errors. Phone backup reuses Mobile access's QR
  renderer with expiry, explicit renewal and dismissal cleanup. Uses the existing fonts, theme,
  controls and icons from Claude's boards A–D. Regression cases in serverSetup.test.mjs and
  environments.test.mjs; verified here with strict TypeScript no-emit and test syntax/whitespace
  checks only. Build/dist, sandboxed suites, companion integration and headless visual verification
  are deliberately left to the host verification session. Summaries describe the latest backend
  snapshot (configuration freshness is recalculated by backend setup/review); OS is shown after
  the existing connection probe. No backend or ptyhost changes.

- Server setup S2: configuration review (`POST .../setup/configuration/plan`) and confirmed apply
  (`POST .../setup/configuration/apply {plan_id}`) mirror agent seeds, referenced Claude files,
  project memory, filtered policy/projects and selected projects' development directories. Paths are
  mapped to the server; the diff names additions, changes, removals, server edits and excluded files
  with reasons/rule names only. Filename/size/link exclusions and `common.secret_findings` run before
  archiving. A changed local snapshot or server manifest refuses the confirmed plan. The bare SSH
  peer validates tar/sha256 and writes files atomically within the account home and mirrored roots.
  `GET/POST .../setup/secrets` discovers extension placeholders and project `.env.example` keys,
  returns only names and set/unset, and writes submitted values directly over the existing peer
  channel (0600, atomic). Server-only placeholder offsets allow rotation without retaining a second
  copy of values; editing the template invalidates those offsets. Values never enter laptop phase
  records or responses. Both steps have real states/actions, local GUI authorization and S1's
  write-ahead/unknown-outcome handling. No shell, sudo, new listener or ptyhost changes.
  Regressions: test_server_config_sync.py, test_server_setup.py, test_piperd.py. Syntax compilation
  and diff whitespace checks verified; suites and live SSH/companion flows await sandboxed verification.

- Server setup phone backup (S5): the `phone` step installs the pinned optional mobile runtime
  into `~/.piper/mobile-venv` and enables a `piper-mobile` systemd user bridge sharing the server's
  own state and loopback API. It uses this computer's configured public relay origin (outbound only);
  missing relay configuration or a private relay capability requires user action. No tokens or keys
  are copied. Setup is idempotent, bounded, shell-free and resumes through S1's write-ahead phases.
  `POST /api/environments/<id>/setup/phone/pair {}` returns the server's existing Settings invitation
  `{url, id, expires}` for a notebook QR and explicit phone backup pairing (single use, 60 seconds).
  This local-GUI-only, no-store response never enters setup records or logs. The bundle adds only
  `mobile-requirements.txt`, never the venv. Regressions cover setup, service content, relay refusal,
  recovery, pairing and endpoint authorization in test_server_setup.py, test_remotes.py and test_piperd.py.
  Verification in this session: Python syntax compilation only; suites and live SSH/systemd/relay/phone
  behavior remain unverified. ptyhost is unchanged.

- Server setup backend (S1): ten-step snapshots and background run/Fix endpoints under
  `/api/environments/<id>/setup`, with per-environment `server-setup/<id>/setup.json` phase records.
  Daemon restart resumes completed phases; an in-flight or lost SSH answer requires read-only
  reconciliation before an explicit retry. Peer operations probe Docker/Hole, agent authentication,
  extensions and projects, install or update a checksummed Hole binary (>= 2.1) into the server home (Hole builds its
  image on the first sandboxed session; setup never starts an agent), generate a server-only SSH key, publish scanned trusted
  extension sources with trust and a user-service drop-in, run bounded argv requirements, and clone
  selected projects into their mirrored layout. No shell, sudo, copied credentials or new listener.
  `server_requirements(server)` is the optional extension hook; only secret names/targets are surfaced.
  Git access pins the forge host keys this computer already trusts (`ssh-keygen -F`, public data) in the
  server known_hosts and is `ok` only when `git ls-remote` reads every selected repository - first with
  the SSH account's own keys and config (they never leave the server; no new key is made then), else with
  a Piper server key. Forges have no generic key registration contract here, so that key is shown
  (`needs_user`) for one-time registration as a user or deploy key; clones use the identity that worked.
  Extension sources are refused on high-confidence secret values (`common.secret_findings`: forge/chat
  tokens, private key bodies, AWS key ids, JWTs, URL passwords), not on code that names passwords;
  `tests`/`e2e`/`development` stay local. Requirement argv allows multi-line programs up to 64 KiB.
  The peer command is kept under Linux's 128 KiB single-argument limit (regression test).
  Configuration and secret writes are provided by S2 above; phone pairing remains deferred to S5.
  Requirement output is withheld rather than exporting arbitrary secret-bearing tool output.
  Regressions: test_server_setup.py, test_remotes.py, test_piperd.py. Verified by the sandboxed core
  suites and the companion extension's requirement rows against this checkout; actual Hole download,
  systemd, SSH clones and agent login probes on a server are verified separately (live setup). ptyhost is unchanged.

- Move to Server GUI: session-menu confirmation with wait/now timing and hidden-path consent,
  per-session progress and recovery strips, capability-aware server selection, and a locked local
  session linking to its server continuation. Incoming sessions show their handoff provenance and
  manual-stop warning, dismissible in the client. Regression scenarios: `move.test.mjs`.
  Verified with the sandboxed unit suites and headless screenshots of the dialog and each strip
  against a demo fixture; a live transfer to a real server remains a manual check.

- "Move to server" backend: `POST /api/sessions/<id>/move {environment, mode: wait|now}`
  asks the agent for `HANDOFF.md` (after its turn, or at once into a running one; never into a permission
  prompt or a half-written prompt), stops the session once the agent finished, commits `wip: handoff <sid>`
  (an ignored `HANDOFF.md` added with `-f`), pushes through the push card and asks the server's peer (new
  op `transfer`: Piper's handoff document and the session facts as a checksummed archive, refused without a
  verified identity) to start the continuing session. The local session ends `moved`, locked for
  start/send and never removed automatically. Phases are recorded in `<session>/transfer.json` and continue
  after a piperd restart; a lost server answer stops as `remote_unknown` and `POST .../move/check` asks the
  server instead of replaying. A manual Stop moves on without `HANDOFF.md` and tells the server session so.
  The server service must be updated (`capabilities.transfer`). Regressions in
  tests/piper-unit/test_transfer.py, test_remotes.py and test_piperd.py.

- Phone level 1 retains the paired primary computer and one explicitly paired backup, migrates
  existing phone profiles, and falls back after a ten-second startup deadline through a full reload.
  Backup mode hides Settings (including pairing, providers and environments), offers manual return
  after a sixty-second hello probe, and keeps independent push subscriptions for each computer.
  Regression coverage includes migration, consent/identity, fallback, recovery, limited UI, forgetting
  the backup and local-only push snapshots. Verified with the sandboxed unit suites and a headless
  render of each new phone state; the end-to-end phone/relay flow and real push delivery remain device checks.

- `propose_session` accepts an optional `agent` (`claude` | `codex`, the caller's agent by default) and an
  optional `worktree` name: the proposed session starts with that agent in the project's linked worktree
  `<project>@<name>`, created with a new branch of that name after the card when missing. The card names
  all three. An unknown agent or worktree name is refused before any card. Regressions in
  tests/piper-unit/test_sessions.py.

- Confirmed pushes set their upstream on Git 2.56 even when Git already advanced the matching
  remote-tracking ref during the URL push. A different concurrent tracking tip is still preserved.
  Regressions cover both cases with real local pushes.

- Database dashboards supplied by extensions follow the selected main/worktree stack and stay visible
  and searchable without SQL connections. Extension service env files accept quoted values followed
  by comments. Docker test runners preserve the host CLI context while mounting requested extra
  sources read-only; host Docker configuration is never forwarded into the runner. Verified with
  sandboxed regressions, a real isolated Docker stack and headless browser checks.

- Follow-up audit review makes grid text conflict checks independent of linguistic collations,
  ignores comment-only SQL selections, and retains the launcher project editor during polling and
  nested confirmations. Git pushes pin the reviewed destination through LFS preparation and transfer.
  Regression checks cover lossy collations, real transaction rollback, local Git remotes and headless
  editor recovery.

- Audit repair integration preserves bidi protection on new dynamic labels and covers terminal
  fixtures and revised UI contracts. Verified with the complete sandboxed Python suite (2,826 cases,
  105 conditional skips), the final client suite (1,214 passes, 2 skips), build freshness checks and
  headless browser regressions. External providers and native desktop behavior remain unverified.

- The session launcher opens project registration without losing task/images and explains unborn
  worktrees before Start. Strict/Direct recovery retries the same session in Hole while retaining
  Strict. Project removals are staged, undoable and confirmed, clear observability mappings and
  suppress immediate rediscovery without deleting files. Approvals share the main column above
  Composer; Another session and Restore previous layout are separate actions. Disabled Beside
  Terminal links to Host shells with a return action, and Settings search stays outside its scrolling
  navigation. Verified with sandboxed Settings/client regressions, real HTTP/PTY fixtures and
  headless desktop/390px checks against Claude-authored designs.

- Empty Files offers New file; HTTP remains available before the first request. Creating a starter
  request opens environment guidance and blocks its unresolved host without dispatching a request.
  Empty environment JSON editors show an unsaved schema example and explicit Save. Verified with
  sandboxed HTTP/filesystem/client suites and a full main-client headless flow; no external request ran.

- Conversation history exposes its total and loads earlier pages with stable cursors and scroll
  anchors; refresh retains expanded history. Queued starts show their task, runtime and worktree
  before removal. SSH attachment loss shows a persistent channel warning and explicit reconnect
  without replaying input or marking an unobserved remote process as exited. Verified with
  sandboxed backend/client suites, real HTTP/WebSocket fixtures and headless browser checks.

- Database metadata has an explicit guarded Refresh that keeps SQL/grid drafts. Empty project
  registries link to project registration; IDEA imports distinguish unreadable files from missing
  sources and refuse unsupported engines. Connection forms fit a 390px viewport. Verified with
  sandboxed database/client suites and 70 headless checks, including real permission recovery,
  external schema changes and persisted mobile form edits.

- Comment delivery preserves source-specific drafts and durable operation receipts; checking a lost
  acknowledgement never posts it again. Feed persistence and audit preflight fail before delivery,
  while history failures after confirmed PTY delivery offer a separate retry. Issue pickers refresh
  changed origins, and comment controls name the actual forge. Verified with sandboxed backend/client
  suites and 22 headless browser checks, including response loss and immediate-send draft races.

- Review metadata reuses its request object-store context, and periodic checks validate the
  repository once while still checking every live file and rejecting repository replacement.
  Content search pins Git's automatic thread count against repository overrides. Verified with
  sandboxed Workbench/Git/filesystem suites and real headless HTTP requests on 35k files and
  64 distinct snapshots; all measured requests met the two-second budget on the tested host.

- Grid title controls receive their first click without pane activation replacing them; an open
  pane chooser refreshes session states. Direct exit messages describe the native process, hidden
  transcript consent names its source agent, and project-key errors explain the per-level length
  limit. Verified with sandboxed client/Settings regressions and headless browser checks.

- Settings conflicts retain dirty values over the current saved baseline for explicit Save or
  Discard. Token verification names the saved token and asks to save token edits first. Automation
  rules reject stale snapshots; presentation-language changes preserve session grants, omitted
  automation start options inherit remembered project defaults, and new DB forms use saved SSH
  defaults without reload. Verified with sandboxed Settings/Inbox/Sessions/client suites and a
  headless browser; external providers and actual model/SSH processes remain unverified.

- Cancelling an active CSV import interrupts its own request and waits for rollback before closing;
  a late successful batch cannot commit it. Console cancellation also covers connection preparation
  and prevents later SQL dispatch. Verified with sandboxed DB/client regressions, a controlled SSH
  wait and headless durable-row checks; external SSH/database providers remain unverified.

- Database grid Submit and console Commit/rollback retain operation identities after a lost
  acknowledgement, replay the completed result and refuse an unknown retry instead of repeating SQL.
  Confirmed rejections remain correctable; new intentional operations get fresh identities.
  Transaction messages distinguish an unknown outcome, and detached grids stop observing resizes.
  Verified with sandboxed DB/client/HTTP regressions and headless durable-write response-loss tests;
  receipts are memory-only and expire, so restart recovery cannot confirm a missing receipt.

- HTTP restores active runs after reload and refuses a duplicate GUI run while the project is busy.
  Files refreshes the HTTP toolbar after saving, historical responses keep their recorded URL,
  and Environment JSON editing preserves the settings draft and participates in the unload guard.
  Verified with sandboxed HTTP/filesystem/client regressions and a headless browser; native dialog
  dismissal and external HTTP/SSH/TLS transfers remain unverified.

- Database consoles pin the displayed schema to their actual transaction and lock schema changes
  while running or open. Selected SQL statements run together with separate result tabs; PostgreSQL
  dollar quotes and quoted completion names stay intact, and composite foreign-key jumps use every
  key column. Both Show DDL paths reload metadata, writes expire metadata caches, and schema failures
  expose Retry. Verified with sandboxed DB/client regressions and a headless two-schema SQLite fixture;
  live external database engines remain unverified.

- Review keeps Undo available after the last reverted block and retains Viewed for the exact
  reviewed version across reloads. New repositories can create their first commit; reviewed commits
  refuse an unfinished Git operation. Push confirms an exact source and destination, rejects stale
  previews and sends only the confirmed commit if HEAD moves later. Verified with sandboxed Git,
  manager, HTTP and client regressions plus a headless browser and local synthetic remote.

- Saving an existing server editor refuses a connection removed by another client, preserves
  the name draft and does not reconnect it. Verified with sandboxed remote/client regressions
  and a headless Servers editor against the real local HTTP API without SSH calls.

- Database connection Test probes drafts without saving credentials, agent access or masking.
  Stale editors and occupied Add/rename names are rejected; IDEA imports distinguish engine
  and endpoint and suggest separate names. Invalid explicit ports no longer use defaults.
  Verified with sandboxed DB/client/HTTP regressions and a headless SQLite-driver fixture.

- Timesheet retries keep the same manual-row ID after a lost response, preventing a duplicate
  durable row; a deliberate new add receives a fresh ID. Verified with the sandboxed timesheet
  suite and a headless browser against the real HTTP handler and local storage.

- New session keeps the first task through project import and checkout selection, protects
  unfinished text and image drafts when leaving, and does not start a session after its launcher
  was cancelled during worktree preparation. Late preparation results leave a newer launcher intact;
  an already running Git preparation may finish without launching an agent. Verified with
  sandboxed client regressions and a headless browser through the full application.

- Files refuses editable text with invalid UTF-8, preserves Undo after a restore collision,
  reports failed explicit reloads without clearing drafts, and keeps symlink Rename in its list.
  Verified with sandboxed filesystem regressions and a headless browser scenario.
- HTTP environment files are sensitive candidates at every project depth. Session transport
  trust still requires its pinned excludes; policy-only hiding cannot authorize hosts, SSH or TLS.
  Disabling response sharing also withholds native failure output, including existing run records.
  Verified with sandboxed HTTP/provider and isolation regressions.
- IntelliJ diagnostics switches to a newly configured MCP URL immediately instead of keeping the
  previous connection until its status cache expires. Verified with sandboxed MCP routing regression.
- Usage includes managed Codex rollout token counts, splits cached input from uncached input,
  and counts cumulative usage only once across repeated events. Verified with sandboxed digest
  and workbench regressions using native-format synthetic transcripts.
- Database grid edits preserve complete large cells, refuse incomplete values and detect concurrent
  changes to edited columns. Binary edits and INSERT exports preserve bytes. HTTP execute/transaction
  routes accept the original and changed values within a bounded 14 MiB JSON body. Verified with
  sandboxed DB/client/HTTP regressions and a headless grid over a synthetic SQLite database.

- New session accepts clipboard images with removable previews and keeps them with each project's
  task draft. Attachments travel atomically with the initial prompt, including inbox and queued starts;
  host-owned originals survive rebuilding the session config. PNG, JPEG, GIF and WebP attachments are
  limited to eight images and 10 MiB in total. Verification: sandboxed server/client regressions,
  GUI build/check, and headless agent-browser clipboard, draft, rejection and retry scenarios at
  1024/1920 px, including a real start with 9.1 MiB of image data. Native Electron verification remains
  for remote CI.

- Desktop: report an unsupported legacy `piper update` immediately when the original service is still answering, without waiting for an unrequested restart. Regression verified through the sandboxed desktop restart suite.

- Unexpected service outages use the shared diagnostics with the Piper logo, observed connection state,
  bounded health checks, details and retry. The web logo is retained before the outage so it remains
  visible without the service. The obsolete desktop offline page and immediate red web
  connection banner are removed. A brief outage leaves the workspace intact; after two seconds a neutral
  reconnect notice appears, and after five seconds the diagnostic overlay opens. Desktop retains explicit
  Start Piper and Show log actions, including extension consent, and uses its local diagnostic view if
  the renderer cannot display the overlay. Healthy renderers, drafts and selections are retained;
  only a crashed renderer reloads on recovery. Verification: desktop/client unit regressions, GUI build/check
  and sandboxed headless agent-browser scenarios with screenshots. Native Electron outage/consent flows
  were updated but require remote CI; no installed application was replaced or live service restarted.

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
