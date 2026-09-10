# Copilot callback ownership and cancellation

Observed 2026-09-09 with SDK **1.0.13**, runtime **1.0.83**, and one short
`gpt-5-mini` turn on the Linux development host. This qualifies the new callback
ownership primitive against the controlled fixture, not the full execution
layer or arbitrary external tools.

## Implemented contract

[`CallbackRegistry`](../../proxy/core/layers/copilot/callbacks.py) owns a separate
asyncio task for each SDK-hosted tool call. Its API is:

- `CallbackRegistry(on_change)`: the synchronous, non-raising callback invalidates
  the coordinator's observation before ownership changes.
- `await run(tool_call_id, async_factory)`: reserves an ID once, starts an owned
  task, and shields it from cancellation of the SDK's waiter. Reusing an ID
  always fails before invoking the factory, including after completion/error.
- `pending_ids`: immutable snapshot of callbacks still owned. Ownership remains
  until actual task settlement, even when the runtime reports idle or its own
  task list is empty.
- `await cancel_all(timeout)`: requests cancellation once per callback and joins
  within the deadline. It returns only IDs whose tasks are confirmed cancelled.
  Resistant callbacks stay owned and pending; returning normally after
  suppressing cancellation does not count as cancelled.

Cancellation proofs are cumulative for a registry's lifetime so a late join
after a previous timeout is not lost. The supervisor must restrict those proofs
to the current open tool IDs. Repeated cancellation calls do not inject another
`CancelledError` into a callback's cleanup. This primitive cannot forcibly stop a
coroutine that refuses cancellation; process-boundary escalation remains a
supervisor responsibility.

The registry retains ID sets, not completed tasks/results or original exception
messages. Callback failures are reported as sanitized `CallbackExecutionError`
with the error's class name and without its original exception context.

## Live result

[`callback_probe.py`](../../scripts/copilot/callback_probe.py) uses the real
source registry as the SDK custom-tool handler. The fixture has no shell,
filesystem, or network operation; it waits on a controller event. The same
isolated home, explicit runtime environment, disabled ambient login/discovery,
exact tool allowlist, and typed permission checks as the lifecycle probe apply.
Only one controlled tool invocation is authorized. No credential values or raw
model/tool payloads are retained.

The [sanitized live report](evidence/copilot-callback-registry.json) confirms:

| Observation | Result |
| --- | --- |
| Runtime abort | `session.idle.aborted=true`; native task list empty. |
| Actual callback at that point | Still owned and running; not finished. |
| Explicit registry cancellation/join | Confirmed IDs exactly match the owned tool-call ID. |
| Callback behavior | Observed `CancelledError`, executed its completion cleanup. |
| Registry after join | Zero pending callbacks. |
| Observation invalidation | Called before registration, cancellation request, and ownership removal. |
| Runtime shutdown | Ordinary SDK cleanup; no tracked descendants survived. |

There was still no native `tool.execution_complete` for the aborted tool. The
new proof comes from joined host callback ownership, not from inventing a native
completion event. A coordinator may use it only with the matching accepted
control request and consistent runtime/host observations.

This addresses the controlled SDK callback gap documented in the
[lifecycle results](copilot-lifecycle-results.md). It does not establish that
cancellation reverses a completed side effect, kills descendants of arbitrary
tools, propagates through MCP servers, or handles remote workers.

## Verification and reproduction

Ten offline tests plus seven parameterized subtests pass. They cover actual
asyncio task ownership, SDK-waiter cancellation, resistant callbacks and
deadline behavior, late cancellation proof, cancellation of the shutdown waiter,
duplicate-ID suppression, sanitized failures, and invalidation ordering.

```bash
python -m pytest scripts/copilot/tests/test_callbacks.py -q
```

The optional live probe defaults to a 90-second deadline (maximum 120) and one
user prompt, with a two-second registry cancellation deadline. It needs the
pinned spike dependencies, provisioned runtime, and an eligible explicitly
selected GitHub CLI identity:

```bash
python scripts/copilot/callback_probe.py \
  --runtime /path/to/copilot-runtime --live --use-gh-token \
  --output /tmp/copilot-callback-registry.json
```

No production engine registration, service deployment, or shared credentials
were changed by this work.
