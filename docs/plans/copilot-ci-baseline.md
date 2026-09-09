# Copilot C0: CI baseline

Recorded 2026-09-09 against the OtoDock 1.6.0 planning baseline. No Copilot
runtime integration is included in these results.

## Fork gates

The proxy, dashboard, and new satellite jobs run for `OtoDock/oto-dock` and
`frostyard/oto-dock`. Other forks remain outside this explicit allowlist.
The satellite job installs its own pinned runtime dependencies and the existing
test dependency set, then runs independently of the proxy's database fixtures.
These CI jobs have read-only repository permissions and need no inference
credentials.

The satellite job currently covers Linux and Python 3.13. It is not evidence
of macOS/Windows compatibility or of the satellite's Python 3.10 runtime floor;
some existing tests directly import Python 3.11's `tomllib`.

## Executed local baseline

Environment: Linux, Python 3.13.5, Node 24.19.0, Ruff 0.15.22. Python
dependencies were installed into a disposable virtual environment from the
committed requirements; dashboard dependencies used `npm ci`.

| Gate | Command | Result |
| --- | --- | --- |
| Workflow syntax | `actionlint .github/workflows/ci.yml` | Passed. |
| Repository lint | `ruff check .` | Passed. |
| Satellite | `python -m pytest satellite/tests -q --timeout=120` | Initial baseline: 507 passed, 2 failed. After repairing test isolation: 509 passed. |
| Audio | `cd audio && python -m pytest -q` | 134 passed, 2 skipped. |
| Dashboard typecheck | `cd dashboard && npx tsc --noEmit` | Passed. |
| Dashboard build | `cd dashboard && npm run build` | Passed; existing large-chunk warning. |
| Dashboard tests | `cd dashboard && npx vitest run` | 123 files, 801 tests passed; jsdom canvas warnings. |
| Proxy | `cd proxy && python -m pytest -n 8 -q` | Did not collect locally: no PostgreSQL listening on localhost:5432. Disposable container provisioning was rejected by the workstation's container image policy. The subsequent fork CI run below establishes this baseline using its PostgreSQL service. |

The two satellite failures were config-file tests that called `CodexSession.start()`
without replacing its daemon connection. They attempted to launch a real
`codex app-server`, despite testing only configuration/auth-file materialization.
Both now reuse the same mocked daemon helper as the neighboring session tests,
return a deterministic thread ID, and close the session. Their file-content
assertions are unchanged. The suite no longer requires a Codex installation or
inference credentials for these tests.

## Executed Frostyard CI baseline

[Run 34405648436](https://github.com/frostyard/oto-dock/actions/runs/34405648436)
passed all three jobs for commit `b74b440` on
[PR #3](https://github.com/frostyard/oto-dock/pull/3). This is executed fork CI,
not a skipped repository guard.

| Gate | CI result |
| --- | --- |
| Proxy against disposable PostgreSQL 16.14 | 7,994 passed, 14 skipped, 88 warnings; 278.64 seconds. |
| Audio | 134 passed, 2 skipped. |
| Satellite | 509 passed. |
| Dashboard | Typecheck/build passed; 123 test files, 801 tests passed. |
| Repository lint | Passed. |

The [release image and update-channel audit](../development/frostyard-releases.md)
records the remaining upstream boundaries and the fork's source-build policy.
Together with the executed CI baseline, this completes C0. It does not establish
any Copilot runtime capability or satisfy the later parity acceptance gates.
