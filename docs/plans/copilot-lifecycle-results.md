# C1 Copilot lifecycle observations

Observed 2026-09-09 on the Linux development host using Python SDK **1.0.13**,
runtime **1.0.83**, and **gpt-5-mini**. These are isolated SDK probes outside
OtoDock's sandbox, not a production execution-layer implementation.

## Method

`scripts/copilot/lifecycle_probe.py` creates an isolated temporary home, Copilot
state, and empty workspace. It uses the explicitly selected GitHub CLI identity
in memory, an explicit child environment, `mode="empty"`, no ambient login,
no configuration discovery, no file hooks, and no host git operations. Only one
known SDK custom tool is exposed for control scenarios. Its permission callback
approves the exact no-argument tool and denies everything else.

The custom `lifecycle_hold` callback performs no filesystem, network, or shell
operation: it blocks on an asyncio event owned by the probe. This lets the probe
issue a control request while a tool is known to be executing, then separately
measure control acknowledgment, callback completion/cancellation, message
delivery, assistant completion, session idle, and task registry state. Reports
retain event names/counts and fixed marker matches, not message contents, task
descriptions, identifiers, or credential values.

Each invocation has a 180-second overall deadline (maximum 240), bounded RPC
timeouts, a 45-second maximum callback hold, and the runtime's minimum accepted
30-credit session limit. Each control scenario sends at most two short user
messages; compaction sends two plus one summary operation; the task-registry
scenario sends no model prompt. Nine invocations were used, including repeated
abort field verification and the interrupt diagnostic iterations below.

## Abort: idle does not prove callbacks have stopped

[Abort evidence](evidence/copilot-lifecycle-abort.json) confirms:

1. The tool callback starts after its permission approval.
2. `session.abort()` returns an acknowledgment; the runtime emits `abort`,
   `agent.interrupted`, `assistant.turn_end`, and `session.idle` with
   `aborted=true`.
3. The SDK-hosted callback is **still running**, has not been cancelled, and does
   not appear in `session.tasks.list()`; the list is empty both before and after
   the abort acknowledgment.
4. The probe explicitly releases the callback, which returns normally. The
   runtime does **not** emit `tool.execution_complete` for this aborted tool.
5. A fresh message is delivered and returns the expected marker. Its subsequent
   `session.idle.aborted` is absent (`null` in the report).

The repeated abort run confirmed the same behavior and retained the explicit
idle flags. Thus neither an abort RPC acknowledgment nor an aborted idle event
proves that SDK-hosted tool work has stopped. An empty native task list also
does not establish that fact.

## Immediate message: accepted before delivered

[Immediate-message evidence](evidence/copilot-lifecycle-immediate.json) shows
`session.send(..., mode="immediate")` returning a message ID while the callback
is held. During the two-second observation window, the callback is neither
cancelled nor finished, the follow-up has no matching `user.message` delivery
event, and there is no session idle.

After explicit callback release, `tool.execution_complete` arrives, then the
runtime moves through assistant turn-end/turn-start, delivers the follow-up,
returns its marker, and emits session idle. The sender must distinguish
**accepted** from **delivered**; `mode="immediate"` does not forcibly interrupt
an in-flight SDK callback in this test.

## Interrupt and background task retirement

`session.rpc.interrupt_main_turn()` returned `interrupted=true` while a callback
was held and a controlled client-owned background task was registered. The
background task stayed `running`, and the callback was not cancelled. The
runtime emitted `assistant.idle` but no `session.idle` while that task existed.

Two diagnostic iterations exposed an additional limitation:

- [Task still registered](evidence/copilot-lifecycle-interrupt-task-held.json):
  awaiting session idle before retiring background work failed after 30 seconds.
- [Task explicitly retired](evidence/copilot-lifecycle-interrupt-retired.json):
  cancelling/removing the client-owned task after releasing the callback still
  did not produce a new session-idle event within 30 seconds.

The first was an incorrect probe ordering assumption; the second is an observed
event gap. A subsequent diagnostic attempted the metadata query using an
incorrect result-field name (`is_processing` instead of `processing`), failed
locally, and was corrected. These are not hidden as passing vendor checks.

The [final interrupt probe](evidence/copilot-lifecycle-interrupt.json) passed:
after explicit callback release and background retirement,
`session.rpc.metadata.is_processing()` returned `processing=false`, and the task
list was empty, while session idle was still absent. A fresh prompt was then
accepted, delivered, and answered correctly; only that subsequent turn produced
session idle. The query is evidence about the local runtime's processing state;
it must not be treated as a native `session.idle` event or as callback-drain proof.

## Task reconciliation

[Task-registry evidence](evidence/copilot-lifecycle-tasks.json) uses a controlled
SDK-owned task, without launching a shell or subagent:

| Operation | Observed result |
| --- | --- |
| Register | `type=client`, `status=running`, `sequence=0`. |
| Repeat same owner task ID | Same canonical task; `created=false`. |
| Publish completed at sequence 1 | Update applied; list reports completed at sequence 1. |
| Repeat same update | `duplicate=true`, `applied=false`. |
| Remove completed task | Removed; task list empty. |

Completed tasks remain listed until removed, so a nonempty task list is not
equivalent to active work. The pinned SDK defines native task statuses
`running`, `idle`, `completed`, `failed`, and `cancelled`; client-owned tasks also
have `orphaned`. Only the running/completed transitions above were exercised.
Native background agents, shell processes, orphan recovery, and cancellation
acknowledgments from real external work remain unverified here.

## Compaction

[Compaction evidence](evidence/copilot-lifecycle-compact.json) confirms
`session.rpc.history.compact()` succeeds, emits compaction-start and
compaction-complete, and preserves the exact marker for a subsequent message.
The runtime reported two messages removed, a generated summary, and
`tokens_removed=-385`: this tiny conversation grew during summarization.
Successful compaction must not be displayed as guaranteed token savings.

## Implications for the production supervisor

- Track accepted prompts separately from delivered prompts, using message IDs.
  Do not settle work or assume steering took effect from `session.send()` alone.
- Own SDK callback tasks explicitly. Abort must cancel/join or otherwise safely
  drain them; runtime task reconciliation cannot substitute for callback state.
- Resolve aborted tool presentation only after confirmed abort and host callback
  drain. Waiting indefinitely for `tool.execution_complete` is insufficient.
- Distinguish `assistant.turn_end`, `assistant.idle`, and `session.idle`. Reconcile
  task statuses, pending permissions/questions, queued messages, and host work
  under a consistent session generation before declaring completion.
- The interrupt event gap requires a separately tested reconciliation path or
  a bounded failure policy. Do not fabricate an idle event from an empty list.
- These probes release the controlled callback themselves. They do not establish
  safe cancellation of an arbitrary shell, MCP, native subagent, or remote task.

All completed probe processes used ordinary SDK shutdown and left no tracked
descendants. This is process cleanup for these fixtures, not a resource-soak or
cross-platform qualification result.

## Reproduce

With the spike dependencies and a matching provisioned runtime:

```bash
python scripts/copilot/lifecycle_probe.py \
  --runtime /path/to/copilot-runtime --scenario abort \
  --live --use-gh-token --output /tmp/copilot-lifecycle-abort.json
```

Other scenarios are `immediate`, `interrupt`, `compact`, and `tasks`. Each
invocation is explicit; there is no automatic paid retry loop.

Offline probe tests require ordinary test dependencies plus `psutil`, without
the SDK, runtime, PostgreSQL, or credentials:

```bash
python -m pytest scripts/copilot/tests/test_lifecycle_probe.py -q
```

Five tests cover report redaction, task snapshot filtering, event timeout
behavior, controlled callback cancellation, and rejection of unexpected tool
arguments. They validate the harness, not the vendor lifecycle semantics.
