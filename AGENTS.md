# Working on Piper

- This is the shared instruction entrypoint for AI contributors; `CLAUDE.md` imports it.
  Read `CONTRIBUTING.md`, `ARCHITECTURE.md` and the relevant test documentation before changing behavior.
  Check the branch, upstream and working-tree ownership first; preserve other sessions' changes.
- Preserve the existing Python standard-library backend and TypeScript client architecture.
- Keep organization-specific integrations outside the core, behind the extension interface
  (`piper/extensions.py`, `PIPER_EXTENSIONS`).
- Include available companion extensions in development, verification and full functional audits.
  Discover their source directories from the user's task, workspace, `PIPER_EXTENSIONS` or registered
  manifests; read each companion repository's `AGENTS.md` / `CLAUDE.md` and extension README. Inspect
  manifests as data; do not import or grant trust to unknown host code just to discover it.
- Before a core change, check its impact on extension hooks and consumers: API/defaults, CLI/bootstrap,
  environment and trust, project/worktree selection, session lifecycle, stack providers and database
  discovery/links. Implement generic contracts here and organization-specific behavior in the companion
  repository. Do not copy private integration code or names into this repository.
- For a change affecting those contracts, verify both the standalone core and the available companion
  extensions against the exact core checkout being changed. Use the companion's documented test command,
  explicitly select this checkout with `PIPER_SRC` when supported, and include its sources read-only with
  `PIPER_SANDBOX_READ`. A full audit also exercises extension-added user flows and core integration;
  an existing compatibility suite alone is not a complete extension audit. Report unavailable sources,
  skipped scenarios and unexercised behavior explicitly instead of silently excluding extensions.
- Use neutral examples and placeholders (`example.com`, `acme/shop`). Never add internal hostnames,
  customer details, personal filesystem paths, runtime state, tokens or transcripts to this repository.
  This covers commit messages and author e-mails too. A maintainer's local content check (git hooks) may
  reject a commit or push: rewrite the content or the commit, never bypass the hooks (`--no-verify`,
  `-c core.hooksPath=...`) and never weaken or remove them.
- Keep ARCHITECTURE.md about the component boundaries; record notable changes and their verification in
  CHANGELOG.md (Unreleased).
- Every application change includes a regression test in tests/piper-unit/.
- Run every test through scripts/piper-sandbox.py (`task piper:test`, `task piper:e2e`); never bypass the OS sandbox.
- Build client changes with `sh scripts/piper-build.sh` and include piper/web/dist/; `--check` verifies.
- Verify changed UI behavior in a browser and capture a screenshot.
  Use headless/offscreen or an isolated display; never open native test windows on the user's working
  desktop, switch Spaces or steal focus. Compare screenshots in the relevant state and viewport.
- Do not stop ptyhost. Restarting the daemon requires explicit user authorization.
- Commit, push, publish and visibility changes require explicit user authorization.
  Before an authorized push, run the relevant checks and content scanner. Afterwards verify the exact
  remote commit and CI results for every repository changed; a successful local suite or push does not
  establish CI success. Investigate local/CI tool-version differences without weakening tests or isolation.
- The project is licensed under Apache-2.0; keep LICENSE, NOTICE and bundled third-party licenses intact.
