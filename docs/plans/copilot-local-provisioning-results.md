# Copilot local provisioning and configuration results

The explicit local preview can now be prepared from a pinned official runtime
archive and opened with an application-owned lifetime. A separate authenticated
builder supplies current agent/payer authorization and native instructions.
Copilot remains unregistered; these services do not expose dashboard chat.

## Verified behavior

The optional SDK supplement installed into a fresh Python 3.13 environment with
the proxy's pydantic 2.11.3 and httpx 0.28.1 constraints. Hash verification,
`pip check`, SDK construction and event serialization passed. No psutil,
native download or inference was required. CI additionally checks the supplement
against the complete proxy environment after its normal regression suites.

The actual official 1.0.83 archive passed checksum validation, private atomic
initialization, read-only load, idempotent reinitialization and SDK preflight.
All 30 retained runtime assets match the previously qualified staged layout's
hashes and sizes. The operator CLI independently initialized and checked the
installation subsequently used for live inference.

**218 focused tests** passed: 72 authenticated builder, 72 execution-layer,
5 provisioned-lifetime, and 69 shared registry/concurrency/consumer tests.
They exercise stale authority and access removal, explicit payer scopes,
visibility and knowledge policy, unsafe/oversized instruction sources,
credential-free config, preflight failures, closing admission, concurrent owner
cleanup, cancelled waiters, startup cancellation and retained failure claims.

**1,100 offline Copilot tests plus 153 subtests** passed, including 50 new
provisioning tests and 7 setup CLI tests. Coverage includes altered assets,
permissions, symlinks/hard links/special files, unsafe paths, bounded archives,
verified immutable input, partial destinations, atomic publication races,
concurrent initialization and import without installed third-party packages.
Ruff and diff checks pass. Full proxy/audio/dashboard/satellite CI is recorded
on the PR, including the new optional SDK compatibility check.

## Live evidence

The [provisioned-layer probe](evidence/copilot-local-provisioning.json) passed
in **56.876 seconds** using SDK **1.0.13**, runtime **1.0.83**, the actual sandbox
resolver, layer, private storage, ownership registry and provisioned-layer
context manager. Three bounded turns established:

1. Initial completion, duplicate-session rejection and exact-owner close.
2. Clean history resume through a newly opened provisioned layer, followed by
   idle credential revocation, held permission denial and uncertain-resume refusal.
3. Context-manager exit while the stream consumer was paused after its first
   text event and still held the public producer lock. Exit joined the active
   runtime and removed its context/claim; the uncertain history could not resume.

All three runtimes exited normally, and all credential observers, contexts,
claims and provisioned lifetimes were closed. No native tools executed.

An [earlier run](evidence/copilot-local-provisioning-timeout.json) completed the
first two checks, then hit a deadline during the third turn with the older
1,000-sentence prompt. All three runtimes and their observers still cleaned up
normally. The retained report does not distinguish waiting for first text from
waiting for context exit. The probe now requests 100 short sentences and records
first-text receipt separately; the bounded retry above passed. This is not a
claim that provider response latency is deterministic.

The live probe uses one actual token with controlled account, network-discovery
and knowledge fixtures. It constructs its fixed config directly; the new builder
is verified separately through storage seams and real security/layer validation.
Neither live evidence nor the builder unit tests establish an end-to-end
PostgreSQL-backed authenticated chat route, OAuth refresh, native search-tool
execution, or graceful preservation of an interrupted partial response.

## Next integration gates

The [contract](copilot-local-provisioning-contract.md) documents setup and APIs.
Next work is routing authenticated chat through these services, account/model
selection, generic queued message and attachment handling, authorized cold-resume
discovery and automatic idle cleanup. MCP/delegation, graceful controls, remote
and terminal execution, organization automation and the full parity matrix remain
open. Nothing in this phase changes the running installation on selfie.
