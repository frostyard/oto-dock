# Copilot observed usage results

Local Copilot conversations now retain observed usage through saving and cold
resume. The dashboard displays provider-reported token counts and native nano-AIU
separately from assistant turns, preserving unknown values and reported zero.
The [contract](copilot-usage-contract.md) defines units, limits and attribution.

## Deterministic validation

**266 HTTP API/service tests** pass. Usage regressions cover durable writes
before SSE/completion, late idle and shutdown reports, duplicate UUIDs,
service replacement, stale callbacks, owner isolation, malformed reports,
queue overflow, storage failures and cancelled close waiting for an accepted
write. A failed native cleanup can still drain its usage writer once the
callback transport is independently proven closed; failed ownership remains
retained.

**60 storage validation tests** pass locally. PostgreSQL tests additionally
exercise idle usage without changing turn-completion flags, concurrent duplicate
insertion, conflicting identity rejection, cross-generation deduplication,
owner isolation and shared history limits. These run in full CI.

**148 factory/layer tests** verify observer installation before native session startup,
null versus zero and strict numeric bounds, unchanged completion behavior,
idle/shutdown callbacks, repeated and conflicting reports, failed shutdown
proofs and retained ownership. Review also found a pre-existing startup rollback
gap: the layer received its owner only after factory success, so failed runtime
cleanup during startup could lose the owning instance. The layer now captures
that instance before resources are allocated; failure retains its ownership.

**132 focused dashboard tests** pass, including exact UUID merging, partial
metrics, overflow, archived/live equivalence, cold resume and late history
refresh. Malformed current-conversation refreshes invalidate usage, stale
responses cannot affect a new selection, and transient network errors preserve
valid observed reports. The full dashboard suite passes **956 tests across
130 files**. **1,287 offline Copilot tests plus 153 subtests**, TypeScript,
the production build, repository-wide Ruff and diff checks pass. Full
PostgreSQL, audio and satellite regression results are recorded on the PR.

## Live evidence

The [sanitized HTTP/native-runtime proof](evidence/copilot-usage.json) passed in
**29.142 seconds** on SDK **1.0.13** and runtime **1.0.83**, using exactly three
inference turns and three runtimes:

- An approved native file write completed. The harness deliberately held one
  real usage callback until the HTTP turn had finished, then delivered it twice
  while idle. The report persisted after `turn_complete`, with one copy only.
- Two observed reports survived closing and replacement of the service/layer.
  Cold resume preserved native history and the selected low reasoning effort.
  Replaying the earlier real report into the new observer left both the saved
  reports and revision unchanged.
- A follow-up recalled the original file marker; a later stream disconnected
  after partial output. Three reports remained readable, matching the captured
  native metric projections exactly. Interrupted history remained unavailable
  for resume; idle access revocation and all existing ownership checks passed.

The observations reported 18,355 input tokens, 287 output tokens, 11,904 cache-read
tokens, zero cache-write tokens and 64 reasoning tokens. These are separate
observed metrics, not an invoice or complete account consumption. Every runtime
closed normally, with no remaining ownership claim or security context.

The probe uses actual cookie authentication, HTTP/SSE, configuration building,
credential leases, private native history and sandbox runtime. User, agent and
account reads are controlled fixtures, and conversation storage is in memory.
Delay and replay were explicitly injected; ephemeral usage is not natively
replayed on resume. This does not qualify live PostgreSQL/browser behavior,
multiple payers, provider cache inclusion, currency conversion or billing
completeness. No deployment was changed.

## Remaining work

General billing/usage records, dollar budgets, quota enforcement, context gauges,
organization-wide attribution reports and the remaining engine/automation
workflows stay open in the [parity matrix](copilot-parity.md).
