# Copilot session coordination foundation

The [coordinator](../../proxy/core/layers/copilot/coordinator.py) combines the
existing event translator with explicit completion checks. It is an unregistered,
SDK-independent component for C3; it does not launch a runtime or expose an engine
in OtoDock. This document describes the caller obligations as well as the tested
behavior. C1 compatibility and full P01–P16 acceptance remain open.

## Completion contract

One event-loop consumer supplies contiguous sequence numbers and serialized
SDK events to `receive_event`. Duplicate IDs do not replay output; sequence gaps,
conflicting sequence assignments or payloads, malformed events, and transport loss prevent
further settlement until the owner explicitly rebuilds the session. Sequence
numbers must come from the actual event router, including the satellite replay
protocol when implemented. Assigning new numbers to conceal missing remote
frames defeats this check.

Canonical payload digests distinguish an identical replay from a changed frame
reusing the same ID, without retaining raw event bodies in the coordinator.

After a genuine runtime idle, the owner captures `begin_reconciliation()` before
awaiting fresh snapshots. It then calls `finish_reconciliation()` with processing
state, all native/client background task states, pending permissions, host tool
callbacks, and queued/dispatching input. Missing observations remain unknown.
Completed, failed, or cancelled tasks may remain in the registry; running, idle,
orphaned, unknown, and duplicate task entries prevent completion.

Every fresh event invalidates an in-flight snapshot. The owner must also call
`invalidate_observation()` before changing host-only state, such as registering
a pending permission callback or dispatching another message. A synchronous
finish accepts only a still-current snapshot and delegates to the translator's
open-tool and duplicate-completion checks. The outbound writer lock covers only
mutation dispatch/acknowledgement; holding it while waiting for the entire turn
would block abort/steer. Event delivery and permission replies remain unlocked.

## Abort and callback ownership

The live [lifecycle probe](copilot-lifecycle-results.md) found that `abort()` can
acknowledge and emit aborted idle while an SDK-hosted tool callback still runs.
That callback is absent from `tasks.list`, and its tool completion event may
never arrive. An empty native task registry therefore cannot establish shutdown.

The coordinator tracks abort requests and ignores stale acknowledgements. Model
iteration boundaries do not reset a pending abort. An acknowledged abort becomes
graceful only with a matching later aborted idle, complete settlement, and
separate affirmative history-preservation evidence. It never invents that proof.

For a known tool whose completion event will not arrive, the owner may provide
`cancelled_tool_ids` only after actually cancelling and joining its callback and
confirming the runtime no longer owns pending execution. With a matching abort
and current settled observations, the translator emits an error tool result
before completion. Unknown IDs, already completed tools, stale snapshots, and
rejected aborts cannot use this path. This reconciliation primitive does not
itself cancel callbacks or prove that an external side effect was rolled back.

## Evidence and limits

Offline tests exercise stale observations, stream gaps, replay, background work,
host-only mutations, callback cancellation, abort races, and writer-lock release
after cancellation. The live [MCP recording](evidence/copilot-mcp-config.json)
fed actual serialized events into the coordinator, fetched task, permission,
queue, and processing snapshots after its checkpoint, and produced **one DONE
and zero replayed DONEs**, with no mapping errors. Its permission callback is
synchronous and it exposes no SDK-hosted tools; cancellation reconciliation is
covered separately by deterministic tests informed by the live abort trace.

This primitive has no durable replay cursor, bounded event retention, process
supervisor, cross-process writer lease, or restart adoption. The separate native
[terminal probe](copilot-terminal-results.md) establishes sequential no-tool
history sharing, not centralized ownership. Fresh activity after an idle still
requires another genuine idle before settlement. The observed interrupt path
that never emits another idle therefore remains a liveness blocker; the adapter
must resolve it without falsely declaring active work complete.
