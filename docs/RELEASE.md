# First public release (0.1.0, 2026-10-23)

The record of what was checked before the repository went public. Later releases use the checklist in
[VERIFICATION.md](VERIFICATION.md). The open items are tracked in the pinned issue
[#3 Known rough edges and open checks in 0.1](https://github.com/Kamest/Piper/issues/3).

- [x] Repository name and ownership confirmed.
- [x] Contributor and rightsholder permissions for the Apache-2.0 release confirmed.
- [x] LICENSE and NOTICE verified; the bundled third-party licenses (xterm.js, CodeMirror) and their
  `VERSIONS` files kept.
- [x] Every tracked file and the whole history inspected for non-public material; the history starts from a
  clean export.
- [x] Maintainers, issue policy and contribution terms in [CONTRIBUTING.md](../CONTRIBUTING.md) and the issue
  templates.
- [x] GitHub Actions green and a passing full local gate (`task piper:e2e`) for the release revision; `http_e2e`
  was skipped on that machine (no `ijhttp`).
- [x] Approval for the initial source push and for making the repository public.
- [x] Private vulnerability reporting enabled (see [SECURITY.md](../SECURITY.md)).
- [ ] The private vulnerability-reporting channel tested with a dummy report (#3).
- [ ] Supported OS, runtime and tool versions documented from actual clean-machine runs (#3).
- [ ] Installation, first-session, update and uninstall instructions exercised on each supported system (#3).
