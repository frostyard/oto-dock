# Copilot account model catalog results

New Copilot chats now offer an explicit account-bound model list rather than a
raw model-ID input. Model selection is invalidated when the user, agent or
account eligibility changes. Saved conversations retain their original model
and can resume without discovering a replacement catalog.

## Deterministic validation

**305 focused backend tests** pass across the HTTP API, chat service and
execution layer. They cover shared capacity, personal account selection,
current authority and exact credential snapshots, late completion,
disconnect/repeated cancellation, timeout, shutdown and failed-cleanup retention.
Actual SandboxConfig/SandboxBuilder tests verify the private empty workspace,
absence of real credential/history mounts, and root identity checks.

**201 focused catalog/layer/private-record tests** pass, overlapping the layer
coverage above. Raw inventory tests reject duplicate or oversized inventories,
coerced field types, invalid labels, nonfinite/boolean/out-of-range multipliers
and malformed policy data. Catalog lifecycle tests cover revocation, changed
security context, cancelled RPCs that suppress cancellation, private allocation
cleanup and failed runtime shutdown. Catalog owners cannot enter ordinary chat
turn/control methods or resume native sessions.

The full dashboard suite passes **905 tests across 129 files**. It covers
explicit loading/selection, disabled and unknown policy, retry/empty results,
account-generation and expiry changes, agent access removal, stale responses,
unmount cancellation and saved-model preservation. TypeScript and the production
build pass. **1,147 offline Copilot tests plus 153 subtests**, repository-wide
Ruff and diff checks pass. Full PostgreSQL, audio and satellite regression
results are recorded on the PR.

An independent review checked API/service admission, authorization and cleanup.
The live revocation test initially exposed an internal cancellation escaping as
HTTP 500. Catalog and service boundaries now distinguish that internal failure
from actual caller cancellation; the final live proof and deterministic
regressions require a sanitized unavailable response.

## Live evidence

The [zero-turn HTTP probe](evidence/copilot-model-catalog.json) passed in
**9.492 seconds**, using SDK **1.0.13** and runtime **1.0.83**:

- The selected account returned 21 models, including selectable `gpt-5-mini`.
  The response arrived only after normal runtime shutdown and private-state
  removal. Cross-origin and unauthorized account/administrator requests were
  rejected before runtime startup.
- A second real `models.list` response was held in the harness while the
  selected account was revoked. The watched lease cancelled that operation,
  cleanup joined it, and HTTP returned the exact sanitized 503 response.
- Exactly two runtimes and two real model-inventory RPCs ran. No native session,
  inference turn or conversation row was created. Every ownership claim and
  security context was released, no new private profile/state/home allocation
  remained, and the actual agent workspace stayed unchanged.

The probe uses real cookie authentication, HTTP, configuration building,
credential leases and sandbox runtime. User/agent/account storage reads and the
conversation store are explicit fixtures; the second RPC response is deliberately
held to inject revocation. This is not a live PostgreSQL, browser, reverse-proxy,
two-payer or vendor entitlement qualification. No running deployment was changed.

## Remaining scope

The [catalog contract](copilot-model-catalog-contract.md) defines the selection
and cleanup behavior. General engine registration, shared payers, persisted
model defaults, reasoning controls, attachments/queues, MCP/delegation,
remote/terminal workflows and organization automation remain open against the
[parity matrix](copilot-parity.md).
