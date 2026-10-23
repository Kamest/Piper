## What changes

<!-- The behavior change and why. -->

## How it was verified

<!-- Commands you ran and what you looked at. A GUI change: a screenshot. -->

- [ ] A focused regression test in `tests/piper-unit/`
- [ ] `task piper:test` passes (and `task piper:e2e` for larger changes)
- [ ] A GUI change updates `piper/web/src` and `piper/web/dist` together (`sh scripts/piper-build.sh --check`)
- [ ] Synthetic data only: no tokens, session state, transcripts or private hosts
