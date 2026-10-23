# Working on Piper

- Preserve the existing Python standard-library backend and TypeScript client architecture.
- Keep organization-specific integrations outside the core, behind the extension interface
  (`piper/extensions.py`, `PIPER_EXTENSIONS`).
- Use neutral examples and placeholders (`example.com`, `acme/shop`). Never add internal hostnames,
  customer details, personal filesystem paths, runtime state, tokens or transcripts to this repository.
- Keep ARCHITECTURE.md about the component boundaries; record notable changes and their verification in
  CHANGELOG.md (Unreleased).
- Every application change includes a regression test in tests/piper-unit/.
- Run every test through scripts/piper-sandbox.py (`task piper:test`, `task piper:e2e`); never bypass the OS sandbox.
- Build client changes with `sh scripts/piper-build.sh` and include piper/web/dist/; `--check` verifies.
- Verify changed UI behavior in a browser and capture a screenshot.
- Do not stop ptyhost. Restarting the daemon requires explicit user authorization.
- Commit, push, publish and visibility changes require explicit user authorization.
- The project is licensed under Apache-2.0; keep LICENSE, NOTICE and bundled third-party licenses intact.
