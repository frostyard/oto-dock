# Pinned native shell contract

This source audit covers `github-copilot-sdk==1.0.13` and the Linux native
runtime `1.0.83`. The source audit makes no inference calls; a separate bounded
execution report is summarized below. A tool result, runtime task status, and termination
of every descendant process are different facts; the APIs below do not make
them interchangeable.

## Sources and limits

The reviewed sources are the installed released wheel's
[generated RPC API](https://github.com/github/copilot-sdk/blob/v1.0.13/python/copilot/generated/rpc.py),
[generated event models](https://github.com/github/copilot-sdk/blob/v1.0.13/python/copilot/generated/session_events.py),
and runtime-distributed `schemas/api.schema.json` and
`schemas/session-events.schema.json`. The shell tool definitions were read from
the previously recorded, sandboxed, no-auth `tools.list(model="gpt-5-mini")`
catalog described in [the native tool contract](copilot-native-tool-contract.md).
The native executable is not published Python source: descriptions and schemas
establish the advertised interface, not implementation-level process guarantees.

| Inspected artifact | SHA-256 |
| --- | --- |
| SDK `generated/rpc.py` | `0dd7ca6eb6c816fb4d1e672eeb8a8c33b755a1b85708cc2d24d820d499cbcb3c` |
| SDK `generated/session_events.py` | `2fa7175a66dbd5d76c9db9d33ff6d3b18dd383fd8065838cbc94893d2eb885aa` |
| Runtime `api.schema.json` | `0d4416b3e042bf0064d5349dc2a30e40f2755f0ece0d9ef046fbbcfebaf4de42` |
| Runtime `session-events.schema.json` | `bea5ebe5060d68b70ce366f46b866d8dd19c252ef1df6445905cd2377954458e` |

## Native tools

These are model-facing tools, callable by an authorized host through
`session.rpc.tools.execute(ToolsExecuteRequest(name=..., arguments=...), timeout=...)`.
Their names and arguments are distinct from the direct `session.shell` RPCs.

| Tool | Required arguments | Optional arguments |
| --- | --- | --- |
| `bash` | `command: string`, `description: string` | `shellId: string`, `mode: "sync" | "async"`, `detach: boolean`, `initial_wait: number` |
| `read_bash` | `shellId: string`, `delay: number` | none |
| `stop_bash` | `shellId: string` | none |
| `list_bash` | empty object | none |

The observed JSON schema does not encode the prose limits on shell descriptions
or waits. The catalog describes a 100-character description limit and
`initial_wait` between 10 and 600 seconds. Its advertised default is 10 seconds;
the lower-level built-in descriptor previously reported 30. Set an explicit
bounded wait. The `read_bash` schema supplies no numeric range for `delay`, so
the host must define and validate its own finite bound.

The catalog describes these behaviors:

- Sync commands can return partial output and a shell ID while continuing to
  run after `initial_wait`. Sync sessions are discarded once the command ends.
- Async commands without detachment remain attached across subsequent turns
  and are terminated at session shutdown. This statement needs process-level
  verification before it can serve as a cleanup guarantee.
- `mode="async", detach=true` intentionally creates independent work that can
  survive CLI exit. On Unix it is described as using `setsid`.
- Each command starts in a fresh process. Reusing `shellId` retains that shell's
  original directory, although `cd`, environment changes and aliases do not
  persist between commands. A current-session cwd check cannot establish the
  origin of an arbitrary reused ID.
- `read_bash` can return accumulated output repeatedly. Its output is not a
  guaranteed append-only stream.
- `stop_bash` claims to terminate the process tree, including detached commands
  identified by their original ID. The schema does not expose a typed join or
  descendant census proof.
- `list_bash` advertises active shell IDs, command, mode, PID, status and unread
  output. Its result has no dedicated generated structured inventory type.
  Exact serialization, task-list agreement, scope and completeness require a
  runtime probe; arbitrary output text must not be treated as a trusted list.

An explicit SDK custom-tool override can replace a built-in even under
`builtin:` source filters. The guarded native-only profile and its resume
provenance requirement remain necessary for all four names.

## Session task inventory and controls

All following methods are under `session.rpc.tasks`; the SDK injects its bound
`sessionId` into each request. The task API is marked experimental.

| Python API | Native RPC suffix | Result and limitation |
| --- | --- | --- |
| `list(timeout=...)` | `session.tasks.list` | `TaskList(tasks=[...])`, the tasks currently tracked by this session |
| `refresh(timeout=...)` | `session.tasks.refresh` | Empty result; refreshes detached-shell metadata known to the runtime |
| `get_progress(TasksGetProgressRequest(id=...), timeout=...)` | `session.tasks.getProgress` | Progress or null if no such tracked task |
| `cancel(TasksCancelRequest(id=...), timeout=...)` | `session.tasks.cancel` | `{cancelled: bool}`; no PID/start-time or join evidence |
| `remove(TasksRemoveRequest(id=...), timeout=...)` | `session.tasks.remove` | `{removed: bool}`; refuses nonexistent or running/idle tasks |
| `wait_for_pending(timeout=...)` | `session.tasks.waitForPending` | Empty result after drain **or internal timeout** |
| `get_current_promotable(timeout=...)` | `session.tasks.getCurrentPromotable` | First sync-waiting promotable task, if any |
| `promote_to_background(TasksPromoteToBackgroundRequest(id=...), timeout=...)` | `session.tasks.promoteToBackground` | `{promoted: bool}` |
| `promote_current_to_background(timeout=...)` | `session.tasks.promoteCurrentToBackground` | Atomically finds/promotes a task and returns it, if one exists |

`wait_for_pending` includes background agents, shells and completion-triggered
follow-up turns. Its internal default timeout is ten minutes, configurable by
`COPILOT_TASK_WAIT_TIMEOUT_SECONDS`. Both its generated result and runtime
schema are empty objects: a successful RPC return does not distinguish complete
drain from that timeout. Bound both the RPC and host wait, then reconcile fresh
state. Do not equate an RPC timeout with cancellation of native work.

The task union contains `agent`, `shell` and client-owned `client` tasks. A shell
row requires `type="shell"`, `id`, `command`, `description`, `startedAt`,
`status`, and `attachmentMode`. Optional fields are `executionMode`,
`canPromoteToBackground`, `completedAt`, `pid`, and `logPath`.

| Field | Declared values |
| --- | --- |
| `status` | `running`, `idle`, `completed`, `failed`, `cancelled` |
| `attachmentMode` | `attached`, `detached` |
| `executionMode` | `sync`, `background` |

`idle` is nonterminal. Completed rows can remain tracked; existing client-task
probes already demonstrated that removal is separate from completion. Native
shell retention/removal still needs its own evidence. Unknown or malformed rows
must fail closed. A shell PID is optional and the runtime schema bounds it to
a positive 32-bit integer, but it is not an ownership capability: there is no
process start time, runtime generation, account, cwd, parent tool call or
explicit shell-ID mapping in the row. The relationship between `TaskShellInfo.id`
and a model-facing `shellId` is not defined by these types.

The wording is specifically an inventory of *tracked tasks*. It does not prove
that every shell descendant, unpromoted sync execution, manual shell RPC or
escaped process appears. Refresh can update known detached work; it cannot be
assumed to discover unknown host processes.

## Events and correlation

`session.background_tasks_changed` has empty data and is explicitly ephemeral
in the runtime schema. Treat it as a snapshot invalidation signal. History
replay cannot reconstruct changes that were never persisted, and the event
contains neither a task ID nor the changed state.

Shell completion uses `system.notification` with
`data={content: string, kind: {...}}`. The two typed shell variants are:

- `kind={type:"shell_completed", shellId: string, exitCode?: integer,
  description?: string}`.
- `kind={type:"shell_detached_completed", shellId: string,
  description?: string}`; this variant has no typed exit code.

`tool.execution_start` supplies `toolCallId`, tool name and optional arguments.
Its optional `shellToolInfo` supplies path/redirection hints, **not** a shell ID.
Arguments only contain `shellId` when it was supplied. The start frame therefore
cannot always bind a newly allocated shell to its eventual task row.

`tool.execution_complete` requires `toolCallId` and `success`. Its optional
`result.contents` can contain `{type:"shell_exit", shellId, exitCode, cwd?,
outputFilePath?, outputPreview?, outputTruncated?}`. The enclosing result also
allows untyped `structuredContent`; the schema does not promise a structured
running-shell handle there. A successful tool response can merely mean that a
background command was started or polled. A `shell_exit` or completion
notification identifies a shell command exit, not termination of all descendants.

`assistant.idle` explicitly allows attached shells/background agents to remain
active. `session.idle` states that no background agents or attached shell commands
are in flight; it does not make that claim for detached shells. Neither event
supplies a complete OS process inventory. `session.task_complete` is an agent
task-summary event, not a substitute shell lifecycle event.

## Abort, interrupt, shutdown and resume

`session.rpc.abort(AbortRequest(...), timeout=...)` returns
`{success: bool, error?: string}`. The convenience `session.abort()` sends the
request but returns no typed result. Neither interface documents a complete
shell process-tree join in its response.

`session.rpc.interrupt_main_turn(InterruptMainTurnRequest(flush_queued=False),
timeout=...)` returns `{interrupted: bool}`. Its contract explicitly preserves
running background agents, sidekicks and promoted attached shells. A false
result means no main turn was processing. With `flush_queued=True`, queued
prompts are preserved for a subsequent turn; false clears them.
`cancel_all_background_agents()` also explicitly leaves promoted attached
shells running. These methods cannot implement stop-all-shells by themselves.

Session shutdown persists state and waits for session-end hooks. The tool's
attached-shell shutdown claim must still be checked against real descendants.
Detached-shell survival is intentional. Conversation resume, an old shell ID
and old task metadata are not proof of process ownership after runtime rotation.
As with the guarded six-tool profile, `continue_pending_work=False` prevents
requesting automatic continuation but does not erase persisted tools or recover
unknown processes. A host must retain verified private profile provenance and
explicitly choose whether any shell state is admissible after resume.

## Separate direct shell RPC

`session.rpc.shell.exec(ShellExecRequest(command=..., cwd=..., timeout=...),
timeout=...)` starts a separate shell pipeline. The request's inner `timeout`
is milliseconds, default 30000; the RPC keyword timeout is seconds. The result
is `{processId: string}`, not a native Bash `shellId` or numeric OS PID.
`shell.kill(ShellKillRequest(process_id=..., signal=...), timeout=...)` accepts
only a process ID returned by this API. Signals are `SIGINT`, `SIGTERM`,
`SIGKILL`, with `SIGTERM` the default. `{killed:true}` means a signal was sent,
not that the process has been joined.

The direct API documentation explicitly warns that process-group termination
does not catch descendants that use `setsid` or change process groups, and that
normal leader exit does not trigger group teardown. This source warning concerns
the direct RPC; it must not be silently claimed as the implementation of
`stop_bash`, nor ignored when considering it as an alternate cleanup path.
`execute_user_requested(command, request_id)` and
`cancel_user_requested(request_id)` are another explicit host execution route.
Their presence does not prove coverage by the native tool policy hook.

## Qualification required before enabling shell work

Use bounded, owned no-auth process fixtures to establish sync-over-wait, async
attached and detached task visibility; exact task-ID/shell-ID correlation; start,
partial-result and terminal ordering; read/list serialization; cancellation
effects and descendant survival; and behavior after shutdown/runtime replacement.
Test a second session to establish scope rather than trusting an ID string.
An ordinary foreground fixture and one with a child are separate obligations.

Admission should initially refuse detached work and arbitrary shell-ID reuse.
Keep native mutation ownership and host snapshot revision fencing around every
awaited control or inventory observation. Reject stale/unknown inventories and
new work arriving during settlement. No single native ACK, task row, exit event
or idle event is a substitute for the existing owned-runtime cleanup boundary.
These are implementation recommendations. The following measurements qualify
specific fixtures and do not establish universal shell ownership.

## Measured native behavior and the process fence

The [native shell probe](evidence/copilot-native-shell.json) passed in 50.419
seconds with SDK 1.0.13/runtime 1.0.83, no supplied credentials and zero model
prompts. It used mandatory sandboxes, six raw `session.tools.execute` cases and
two guarded adapter cases. Both runtimes closed normally and no controlled
fixture process survived cleanup.

The raw cases established the following for the exact controlled commands:

- Each active shell had a tracked task whose ID matched its shell ID. In all
  six cases the task's PID matched the fixture's innermost `NSpid` value, **not**
  its host PID or an ancestor PID. Never signal a task PID as a host process.
- A short sync command exited naturally and its task disappeared. The process
  was dead when the direct RPC returned; requiring a retained terminal task
  would prevent normal settlement.
- Cancelling a sync task returned `cancelled=true` and a fresh task snapshot
  showed `completed` while the exact fixture process was still alive. It was
  dead at the subsequent quarter-second check. Native ACK plus terminal status
  is therefore not a join, even with a fresh task read.
- Explicit promotion changed a waiting sync task to background execution and
  released the waiting direct RPC. The raw sync call had still been pending
  after twelve seconds despite `initial_wait=10`; the model path must be
  qualified separately from that direct-RPC behavior.
- `stop_bash` removed its task and the single controlled process was dead on
  return. This does not prove termination of arbitrary descendants.
- An attached async command survived raw abort even though abort returned
  success. Raw interrupt returned `interrupted=false` because no model turn
  was processing, and also left the command alive. Fallback task cancellation
  could still return before process death. These cases do not prove recovery
  of a subsequent model turn after raw abort.

The list/read/stop results were text-oriented `textResultForLlm`/`sessionLog`
with telemetry, not a typed complete process inventory. Discovery emitted task
change and partial-result events but no model tool-start/tool-complete or
session-idle sequence. Consequently this report alone does not qualify model
turn completion or event ordering.

The guarded adapter adds a runtime-owned process fence. Its trusted owner
captures a baseline immediately after runtime startup and before creating an
SDK session. The check compares discovered live host `(pid, start_ticks)`
identities against that baseline; every additional identity blocks settlement,
including newly created persistent helpers. It binds one observed SDK session
object and rejects missing/replaced session ownership. The initial empty SDK
session registry check is not permission to execute raw work before capture or
replace sessions between checks.

Native terminal tasks remain unsettled while that process check reports work.
A previously observed task that disappears may become host-only `RETIRED`
only after the process fence reports settlement. Retirement means its owned
work is no longer present; it does not supply a successful tool result, exit
code or native completion event. Native input cannot assert that host-only
state. Two serialized process censuses are conservative observations, not an
atomic inventory of arbitrary daemonized or undiscovered descendants.

The guarded natural-completion and abort cases both demonstrated an unsettled
snapshot while the fixture was alive, followed by a settled process fence,
dead fixture and settled snapshot. Adapter abort returned only after these
checks. The guarded abort case explicitly promoted its exact task to background
execution first: the tool RPC returned while the process was alive and the
process fence still blocked. Adapter abort subsequently waited for task and
process settlement before returning. This establishes why tool-return state
alone is insufficient even under the guarded profile. Model-facing `read_bash`,
`stop_bash` and `list_bash`, async/detached admission, and arbitrary shell-ID
reuse remain disabled in the production policy. This evidence supports the
narrow attached-shell reconciliation primitive, not broader background or
terminal parity.

## Model-turn settlement and missing tool completions

The separate [model-driven shell report](evidence/copilot-native-shell-model.json)
passed in 76.232 seconds. It used three bounded authenticated turns, each in
its own guarded sandbox runtime/session: natural completion, accepted abort,
and accepted interrupt. Each observed a running native shell task and produced
exactly one OtoDock `DONE`, with no live controlled fixture process and no
pending host requests at completion. All runtimes closed normally. This is not
a cold-resume or multi-turn same-session shell-recovery test.

The native event counts contain three `tool.execution_start` frames but only
one `tool.execution_complete`. Controlled shell termination therefore needs its
own proof to close still-open tool displays. The adapter supplies this proof
only after accepted control, terminal or host-retired task inventory, and an
owned process fence reporting settlement; a new submission clears it.

The supervisor limits this evidence to previously observed, still-open
top-level `bash` tool IDs. It keeps native shell proof disjoint from joined
SDK callbacks and correlated native permission cancellations. The coordinator
requires either an acknowledged matching abort, an observed aborted native
idle, and a current fully settled snapshot; or an acknowledged matching
interrupt and two equal fully settled observations with an intervening
`is_processing=false` barrier. Fresh events or host mutations invalidate the
checkpoint. The translator validates the known open Bash IDs before emitting
an error `TOOL_RESULT` for stopped work and then settling the turn. It does not
invent a successful execution or a native completion event.

These checks preserve the distinction between stopping owned work, preserving
conversation history, and undoing earlier effects. The report demonstrates the
three controlled model paths; it does not expand admission to detached shells,
arbitrary process escapes, model-facing shell controls or terminal parity.
