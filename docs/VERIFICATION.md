# Release verification

The checklist for every release and for every change to installation or upgrades. Copy it into the release's
tracking issue and tick it there; this file stays unticked. Use synthetic projects and isolated test homes;
never put local configuration or runtime state in a source archive. The record of the first release is in
[RELEASE.md](RELEASE.md).

## Core and extensions

- [ ] Verify startup and the first session without extensions or private configuration.
- [ ] Verify optional extensions separately in their owning repositories.
- [ ] Ensure absent backend feature keys disable their client UI.
- [ ] Preserve path protections for the core and extension checkouts.

## Build and tests

- [ ] Verify vendored asset checksums and `sh scripts/piper-build.sh --check`.
- [ ] Run `task piper:test` through the test lock and OS sandbox.
- [ ] Run the required `task piper:e2e` suites and inspect every skip.
- [ ] Fail release verification when a required tool or suite is missing.
- [ ] Verify GitHub Actions for the exact release revision.
- [ ] Exercise installation, first session, update and uninstall instructions on supported systems.

## Services and updates

- [ ] Verify macOS and Linux/WSL service installation.
- [ ] Verify a session-preserving daemon restart in an isolated test installation.
- [ ] Preserve developer branches and dirty checkouts during updates.
- [ ] Pin a tested release and document minimum extension compatibility.
- [ ] Update a user's running service only with explicit authorization.
- [ ] Keep the PTY host running while updating the daemon so sessions survive.
