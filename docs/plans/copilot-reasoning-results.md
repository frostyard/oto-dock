# Copilot reasoning effort results

Local Copilot chat now offers each model's advertised reasoning levels, stores
the selected level with the conversation, and restores it on cold resume.
Existing conversations retain Model default behavior and their legacy native
profile digest. The [contract](copilot-reasoning-contract.md) defines validation,
compatibility and the remaining scope.

## Deterministic validation

**257 HTTP API and chat-service tests** pass, including all five levels, null
and omitted defaults, invalid types/values rejected before startup, saved
metadata, service replacement and immutable resume. **51 store validation tests**
pass locally. PostgreSQL round-trip, migration idempotency, transcript preservation
and database-constraint tests run in the full CI suite.

**387 focused runtime/configuration/catalog tests** pass. These verify strict
raw inventory normalization, explicit SDK create/resume options, unsupported
level rejection before native session creation, profile mismatch rejection and
an independent reference calculation of the original default digest.

**103 focused dashboard tests** pass across catalog selection, preview, agent
routes and saved history. They cover advertised choices, malformed metadata,
scope changes, Model default, immutable saved values and resume requests without
overrides. The full dashboard suite passes **926 tests across 129 files**;
**1,209 offline Copilot tests plus 153 subtests** pass. TypeScript, the production
build, repository-wide Ruff and diff checks pass. Full proxy/PostgreSQL, audio
and satellite regression results are recorded on the PR.

## Live evidence

The [sanitized HTTP probe](evidence/copilot-reasoning.json) passed in
**33.627 seconds** with SDK **1.0.13** and runtime **1.0.83**. It used the actual
account catalog to confirm `gpt-5-mini` advertises `low`, then exercised exactly
three inference turns across three runtimes:

- Create with explicit low effort, approve a native file write over HTTP, save
  the completed conversation and close the owner.
- Replace the service and layer, cold-resume the saved conversation with low
  effort, and verify the follow-up remembers the first turn's file marker.
- Create another low-effort conversation, disconnect during streamed output,
  and verify cleanup and readable, non-resumable interrupted history.

Captured SDK options, raw native create/resume payloads and the runtime's own
`session.model.getCurrent` responses all confirmed low effort. Metadata retained
the setting. Invalid effort and resume overrides returned 422 before startup;
stale revision/handle and wrong-agent protections also passed. Normal cleanup
released every ownership claim and security context.

The probe uses real cookie authentication, HTTP/SSE, configuration building,
credential leases, private native history and sandbox runtime. User, agent and
account reads are controlled fixtures; conversation storage is in memory. It is
not a live PostgreSQL, browser, reverse-proxy, multiple-payer or all-level/model
qualification. No running deployment was changed.
