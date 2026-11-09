# HANDOFF – Server setup (remote coding)

## Goal
Make the automated Settings › Servers setup fully work for the configured server environment.
Last requests:
- copy real `.env*` files and build settings values (Maven `settings.xml`) from this computer to the server on click;
- fix the Phone backup row, which kept asking to pair after pairing.

## Done (pushed to `origin/main`, deployed in the main checkout, piperd restarted)
- `48202d5`: Secrets lists real `.env*` files of mirrored projects, also without a template, and copies them on
  Copy N to Server.
  - Extensions may declare where this computer keeps a secret in an XML file
    (`local: {file, xml: [paths]}`, gated by `extensions.SERVER_SECRET_LOCAL`); the value is used only when all
    declared places agree.
  - The peer writes `.env.*` as key=value lines.
- `211cfc9`: the peer inspects up to 50000 manifest/baseline entries (was 10000); writes stay at 10000.
- `f5dd75f`: peer `mobile_status` returns `paired` (count only); the Phone backup row says
  "Paired as backup on N phone(s)".
- Companion extension (private repository, see its own AGENTS.md): build settings secrets declare XML locators,
  gated so older pinned cores do not receive `local`. Pushed, its pipeline is green.
- Server: Update server done, configuration applied, 10 project `.env` values copied. All 10 setup steps are ok.

## Verified (commands)
- Python: `python3 scripts/piper-sandbox.py -- python3 tests/piper-unit/run_parallel.py -j 2` → 2938 OK.
- Node: `timeout 580 python3 scripts/piper-sandbox.py -- sh -c "node --test --test-concurrency=2 tests/piper-unit/*.test.mjs"`
  → 1272/0.
- Companion suite: run against the exact core with `PIPER_SRC`; 2 unrelated failures need core capabilities that exist
  only in another session's uncommitted changes.
- GitHub CI green for `48202d5`, `211cfc9`, `f5dd75f`.
- `GET /api/environments/<eid>/setup`: all steps ok.
- Copied values compared only by sha256: all match, files are mode 600; no value in setup state, logs or UI.
- Screenshots: `development/server-setup/screens/` (`impl-secrets-copy-*.png`, `impl-phone-paired.png`).

## Remaining / not verified
- Copying build settings values on the real server was not exercised: they were already set there.
  - One repository URL key cannot be found locally and is typed by hand.
- Phone backup pairing fallback not tested on a real device.

## Open questions
- Should the build repository URL be derived from local repositories? It is not reliable today.

## Pitfalls
- **The main checkout holds ~112 uncommitted files of another session.** A session move commits every uncommitted
  file as WIP. Do not move this session from the main checkout without deciding about those files first.
- Deploy into the dirty main checkout:
  1. `git apply --exclude=CHANGELOG.md` the worktree diff;
  2. add the CHANGELOG entry;
  3. `git reset -q <sha>` via git_run;
  4. check that `git status --porcelain` is unchanged;
  5. restart piperd.
- Peer changes take effect only after Update server.
- Tests only through `scripts/piper-sandbox.py`. Never bypass git hooks.
- Never print secret values; compare hashes.

## In progress
Nothing. The worktree `.claude/worktrees/server-setup-fix` is clean at `f5dd75f`.
