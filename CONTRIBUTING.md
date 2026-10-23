# Contributing

Development happens in this repository on `main` through reviewed pull requests.

- Describe the behavior change and how you verified it.
- Include a focused regression test (`tests/piper-unit/`: python for `piper/*.py`, `*.test.mjs` for the GUI).
- Run `task piper:test` (and `task piper:e2e` for larger changes). Tests run only inside the
  repository's OS sandbox, outside any agent sandbox.
- A GUI change updates `piper/web/src` and the built `piper/web/dist` together
  (`sh scripts/piper-build.sh`, then `--check`).
- Use synthetic fixtures and neutral example hosts. Never include session state, private
  infrastructure details or credentials in code, examples, test results or reports.

By submitting a contribution you agree that it is licensed under the Apache License 2.0
(section 5 of [LICENSE](LICENSE)).
