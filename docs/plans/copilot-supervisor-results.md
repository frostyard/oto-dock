# Copilot sandbox session supervisor

This slice connects the previously separate runtime, callback, event and control
primitives into an executable session supervisor. It remains **unregistered**:
there is no dashboard engine selector, account provisioning, or release package
for Copilot yet. SDK 1.0.13/runtime 1.0.83 remain development dependencies.

## Implementation boundaries

- [`SandboxedCopilotRuntime`](../../proxy/core/layers/copilot/runtime.py) requires
  the real `SandboxBuilder`, the private-network launcher, and a read-only mount
  containing the provisioned runtime and adjacent assets. It selects an explicit
  inference token, rejects ambient token/loader injection, disables automatic
  downloads, and validates the pinned SDK/runtime/protocol. Its Linux launcher
  creates a private nonce/PID/start-time ownership record; pidfds bind signals
  to exact process identities. It never kills all children of the proxy.
- [`CallbackRegistry`](../../proxy/core/layers/copilot/callbacks.py) owns each
  SDK-hosted callback independently of the native task list. Duplicate IDs cannot
  execute twice, cancellation of an SDK waiter cannot erase ownership, and a
  bounded cancellation/join returns only confirmed cancellation evidence.
- [`CopilotSdkSession`](../../proxy/core/layers/copilot/sdk_session.py) adapts the
  pinned public session RPCs into processing, task, permission and queue snapshots.
  Unknown task states cannot settle. Queue observations retain occupancy rather
  than private prompt text.
- [`CopilotSessionSupervisor`](../../proxy/core/layers/copilot/supervisor.py)
  streams one producer at a time, correlates accepted versus delivered input,
  routes events through the coordinator, and reconciles native plus host work.
  Outbound locks cover dispatch/acknowledgement, so controls and callback replies
  can progress during a turn. Explicit host permission/question inventory is a
  caller obligation; missing state is unknown, not empty.

Event buffers and RPC/callback waits are bounded. A deadline belongs to the
supervisor rather than an async-generator caller, so a paused consumer does not
keep owned inference running past timeout. Invalid frames, queue overflow and
uncertain acknowledgements start cleanup even when no consumer is draining
events. Repeated caller cancellation cannot cancel shared cleanup. After an
accepted stop/interrupt, new steering must wait for settlement; it cannot erase
the control proof needed to resolve an interrupted tool. Callback admission is
paused before accepted-control cancellation and reopens only for the next
settled independent stream. Shutdown closes admission permanently before taking
its cancellation snapshot, so a late SDK request cannot start host work while
disconnect or runtime cleanup is awaiting completion. Refused callback IDs
remain consumed and cannot execute if replayed after reopening.

## Interrupt completion without a new native idle

The earlier [lifecycle probe](copilot-lifecycle-results.md) demonstrated that
`interrupt_main_turn` can stop processing without emitting another `session.idle`.
The new coordinator provides a separate explicit interruption boundary; it
does not synthesize a native idle event.

The owner records an interrupt ticket before the RPC and requires an affirmative
acknowledgement. After callbacks are cancelled/joined, it captures a revision
checkpoint, reads a complete settled snapshot, checks a separate processing
barrier, and reads a second complete snapshot. Both must agree, the barrier must
say processing is false, and no event or host mutation may have invalidated the
checkpoint. Running, idle, orphaned or unknown tasks, pending requests, queued
input and unresolved tools prevent completion. Native idle and explicit interrupt
settlement use the same duplicate-DONE gate.

Known cancelled tool displays receive error results only after actual host
callback cancellation/join. An RPC acknowledgement or an empty native task list
cannot provide that evidence. Completion here does not establish preserved
partial history or rollback of an external side effect.

## Integration findings

The first combined sandbox test completed its normal turn, then rejected abort
on the second turn before sending the RPC. The
[sanitized diagnostic](evidence/copilot-supervisor-turn-id-reuse.json) confirms
that Copilot reused its model iteration ID across two user inputs. Global
iteration-ID deduplication had incorrectly hidden the second turn start. A new
submission now resets only iteration-boundary deduplication; event replay
fingerprints and message/tool tombstones remain intact. Tests cover reused IDs
across submissions and harmless duplicates within one submission.

The real sandbox shutdown check also showed that `SDK.stop()` can leave
bubblewrap and the runtime alive after stopping their outer launcher. The owner
now sends SIGTERM to the remaining verified processes and allows a bounded grace
period before escalating to SIGKILL. A no-auth sandbox ping/shutdown passed this
path without forced cleanup. Early launcher exit, partial startup, transport
loss, repeated cancellation, ownership-observation failure and unrelated-process
isolation have separate deterministic tests.

## Combined live result

The [sanitized passing recording](evidence/copilot-supervisor.json) uses the
actual sandbox and source components with SDK 1.0.13/runtime 1.0.83. The final
three-turn run passed in 43.994 seconds:

| Scenario | Observed result |
| --- | --- |
| Normal turn | Expected marker; one DONE. |
| Abort during a host callback | RPC accepted; callback cancelled and joined; one error tool result and one DONE. |
| Interrupt with a running background task | RPC accepted; callback cancelled and joined; completion blocked until task retirement, then one error tool result and one DONE despite zero new native idle events. |
| Session shutdown | No owned runtime processes or callbacks remain; no SIGKILL required. |
| Multiple user inputs | Model iteration IDs reused across all three inputs without suppressing the later turn boundaries. |

The complete offline Copilot suite passes 218 tests plus 67 subtests. Regression
coverage includes stale observations, rejected controls, repeated
cancellation, paused consumers, deadline expiry, bounded queues, unknown request
state, exact process ownership and startup/shutdown failures. Full repository CI
runs these tests without installing the SDK or using inference credentials.

## Reproduce

Use the proxy dependency environment plus the pinned development SDK and
provisioned runtime, on Linux with bubblewrap, pasta and pidfd support:

```bash
python scripts/copilot/supervisor_probe.py \
  --runtime-dir /path/to/copilot-runtime/prebuilds/linux-x64 \
  --live --use-gh-token --output /tmp/copilot-supervisor.json
```

The harness creates disposable platform configuration before importing the
sandbox builder; production components never change global platform paths. It
selects the current GitHub CLI token internally without serializing it. Three
short `gpt-5-mini` prompts exercise normal streaming, abort, and interruption with
a controlled background task. Each turn is bounded, the full test has a
180-second work deadline, and the session uses the runtime's minimum accepted
30-credit ceiling. This is not a claim of a smaller enforced charge cap.

The only tool is a trusted Python callback that waits for cancellation and
performs no filesystem, network or shell operation. **SDK-hosted callbacks run
in the host Python process**, outside the runtime sandbox. Production callbacks
must be trusted platform code with their own policy and side-effect controls.
The background task is controlled registry metadata, not a real shell/subagent.

## Remaining gates

This establishes an implementation path for local controlled sessions, not full
P01–P16 acceptance. Still required: account/payer isolation and refresh,
provisioning and production dependency packaging, permission/question bridges,
native tools and background processes, HTTP/brokered MCP flows, cross-process
writer leases, durable replay/retention, cold/warm recovery and remote adoption,
attachments and context/goals, terminal policy, and all supported operating
systems. The current runtime owner intentionally requires Linux pidfd support.

Cooperative Python cancellation cannot prove arbitrary external work stopped or
was undone. A callback that resists cancellation remains owned and prevents
successful cleanup. The supervisor's in-memory ownership and bounded event queue
do not provide a durable side-effect journal or exactly-once execution guarantee.
