# Copilot compatibility inventory

Reviewed 2026-09-09. This is the C1 source-contract inventory for
[the parity plan](copilot-parity.md), not a claim that integration or authenticated
compatibility tests have passed. Runtime observations belong in
[the spike results](copilot-spike-results.md). API presence establishes a candidate
implementation path; it does not establish enforcement or production behavior.

## Version decision and evidence

Use the **published Python SDK 1.0.13 and its pinned runtime 1.0.83** for C1.
Do not follow SDK `main`, install the latest CLI independently, or copy older
dictionary-style Python constructor examples into the adapter.

| Item | Inspected value |
| --- | --- |
| Distribution | `github_copilot_sdk-1.0.13-py3-none-any.whl` |
| Wheel SHA256 | `941dd5b55cf32ba55c73c651052a4a52b259b470c68bf6a6ac3d240c235402c9` |
| SDK release tag commit | `f13e4a2cc7e4e220974d2333142234e162a3252e` (`v1.0.13`) |
| Wheel runtime pin | `copilot/_cli_version.py`: `CLI_VERSION = "1.0.83"` |
| SDK protocol | `copilot/_sdk_protocol_version.py`: version `3` |
| Linux x64 unified runtime archive | `github-copilot-1.0.83-linux-x64.tgz` |
| Archive release SHA256 | `888f8fbb4575c335afba4a8863c647ef04f81e5124c7c794bdcaee90c5fa4503` |
| Runtime publication | 2026-09-04 |
| Python floor | 3.11; older satellites require explicit provisioning or an upgrade |

Provenance: [published package metadata](https://pypi.org/pypi/github-copilot-sdk/1.0.13/json),
[SDK tag](https://github.com/github/copilot-sdk/tree/f13e4a2cc7e4e220974d2333142234e162a3252e),
and [runtime release](https://github.com/github/copilot-cli/releases/tag/v1.0.83).
The wheel was downloaded and unpacked for inspection without accessing credentials
or requesting inference. The release API advertised the runtime archive digest;
the separate executable probe must verify downloaded bytes against the checksum.

The wheel's platform table contains Linux x64/arm64, Linux musl x64/arm64,
macOS x64/arm64, and Windows x64/arm64. This is packaging evidence, not a passing
OtoDock platform matrix. The SDK source is public; existence of generated runtime
RPC declarations does not prove the closed runtime honors every declaration.

Most mappings below come from the wheel's `copilot/client.py`, `session.py`,
`generated/rpc.py`, and `generated/session_events.py`. Corresponding tagged source:
[client](https://github.com/github/copilot-sdk/blob/v1.0.13/python/copilot/client.py),
[session](https://github.com/github/copilot-sdk/blob/v1.0.13/python/copilot/session.py),
[RPC definitions](https://github.com/github/copilot-sdk/blob/v1.0.13/python/copilot/generated/rpc.py),
[events](https://github.com/github/copilot-sdk/blob/v1.0.13/python/copilot/generated/session_events.py).
Generated RPC groups explicitly marked experimental need pinned contract tests;
do not call `_Internal*` APIs to fill a product requirement silently.

## Transport and ownership

The selected path is `RuntimeConnection.for_stdio(path=..., args=...)` with an
explicit sandbox launcher. The SDK appends its runtime options, including
`--headless` and `--stdio`. Its constructor accepts keyword arguments, including
`env`, `working_directory`, `base_directory`, and `use_logged_in_user=False`.
`base_directory` sets the child's `COPILOT_HOME`. Provision the runtime during
installation and disable first-session downloads. Preserve its adjacent native
library/assets: copying just an executable is insufficient.

Use one independently supervised runtime per OtoDock session for the first
integration. Persist engine session ID, isolated config/state location, payer
identity, and event cursor separately from `codex_thread_id`. Explicit child
environments must remove ambient inference credentials while providing selected
repository and MCP credentials through their intended channels. An inherited
`GH_TOKEN` must not accidentally choose the inference payer.

The in-process FFI transport shares the Python host and cannot honor per-client
environment or working directory settings. It is not the sandbox integration
candidate. The SDK's remote/cloud sessions are also not OtoDock satellites:
satellites should host local Copilot runtimes under OtoDock's worker protocol.

## ExecutionLayer method and control map

Names on the left refer to [ExecutionLayer](../../proxy/core/execution_layer.py).
An SDK method below is available in the inspected wheel unless identified as an
OtoDock responsibility. All runtime semantics still require integration tests.

| OtoDock operation | Copilot candidate | Required proof or implementation |
| --- | --- | --- |
| `start_session` | `client.start()`, `create_session(...)` | Sandbox argv/env, isolated state, explicit system prompt, MCP, selected account, launch timeout, cleanup after partial startup. Install event handler before first send. |
| `send_message` | `session.send(prompt, attachments=..., mode=...)`, `session.on(...)` | Queue events into one Oto producer; correlate accepted message ID, turns, and tool IDs. Streaming finals must not repeat deltas. |
| `abort` | `session.abort()` | Determine retained partial history and actual background cancellation semantics before returning Oto's graceful `True`. An RPC acknowledgement alone is insufficient. |
| `steer` | `send(..., mode="immediate")`; queue RPCs | Prove whether input steers the existing turn or queues/intercepts it, including end-of-turn races. Do not return `True` if Oto must still deliver the text. |
| `interrupt_for_queued` | `session.rpc.interrupt_main_turn(...)` (experimental) | Source contract explicitly preserves background work. Prove partial history and no duplicate next-message delivery; do not substitute process termination. |
| `compact` | `session.rpc.history.compact(...)` | Return post-token count only when known; consume success/failure events and exclude concurrent sends. |
| `close_session` | `session.disconnect()`, `client.stop()` | Disconnect detaches handlers and preserves disk history; distinguish detach, runtime shutdown, and destructive `delete_session`. Verify all descendants stop when ownership ends. |
| `respond_permission` | Resolve pending callback future, or `rpc.permissions.handle_pending_permission_request(...)` | Stable request IDs, one response, revocation/timeout handling, unattended denial and reconnect reconciliation. |
| `change_model` | `session.set_model(...)`, `rpc.model.switch_to(...)` | Handle pending versus committed model changes and context-compaction preflight; discover allowed models under selected identity. |
| `change_mode` | `rpc.mode.set(...)` plus permission authority | Native `interactive`/`plan`/`autopilot` modes are not Oto permission modes. Map `default`/`auto`/`acceptEdits` through policy, not spelling. |
| `send_control_request` | Typed `rpc.model.set_reasoning_effort`, mode, history, plan, objective, permission and task APIs | Explicit allowlisted dispatch, runtime errors surfaced, no successful no-op for required controls. |
| `get_session`, `is_session_alive`, `is_session_process_dead` | Oto wrapper around process and SDK handles | SDK ping tests responsiveness, not safe proof of process death. Track process identity/exit and transport closure separately. |
| `session_lock` | Oto `asyncio.Lock` | One writer across producer, compaction, handoff and recovery; permission replies must remain possible while a producer owns the turn lock. |
| `prepare_resume`, `can_resume_session` | `get_session_metadata`, `list_sessions`, `resume_session`, persisted events | Rebuild from durable IDs/config; re-register hooks/tools/callbacks. Metadata existence alone does not prove useful conversation history. |
| `wait_for_bg_subagents` | `rpc.tasks.list`, `rpc.tasks.wait_for_pending` plus shared registry | SDK wait includes agents, shells and completion-triggered follow-up turns. Apply Oto time limits and inspect unsettled state before finalizing delegated output. |
| `drain_bg_commands` | `rpc.tasks.refresh`, `rpc.tasks.list` | Keep event listener alive between user turns and reconcile task state by stable IDs. |
| Remote liveness/replay methods | Oto satellite protocol | Transport sequence numbers, retained event buffers, reconnect adoption and uncertainty handling are platform work, not SDK resume. |

The SDK has broader controls than its short README suggests. Conversely,
`LayerCapabilities` must describe tested product behavior; finding an RPC method
does not justify enabling the corresponding flag.

## CommonEvent map

Contract: [CommonEvent](../../proxy/core/events/common_events.py). Copilot event
objects carry UUID `id`, timestamp, optional `parent_id`, `agent_id`, and
`ephemeral`. Preserve original IDs in adapter state for replay deduplication;
do not use wall-clock timestamps as unique event identities.

| CommonEvent | Copilot source and translation rule |
| --- | --- |
| `TEXT` | `assistant.message_delta`; final `assistant.message` only supplies missing/non-streamed content. Partition subagent output from top-level output using agent/parent IDs. |
| `THINKING` | `assistant.reasoning_delta`/`assistant.reasoning`; synthesize start/end boundaries and expose only reasoning the runtime actually sends. |
| `TOOL_USE`, `TOOL_INPUT`, `TOOL_RESULT` | `tool.execution_start` supplies call ID, tool name, arguments and MCP identity; `tool.execution_complete` supplies success/result/error. Partial-result/progress events can update details. Correlate IDs before rendering. |
| `PERMISSION_REQUEST` | `permission.requested` and callback request metadata; resolve via authority. A permission request can cover a different granularity than a tool call. |
| `QUESTION` | Legacy user-input or elicitation callback; preserve options/schema and cancellation. Bridge exit-plan requests independently. |
| `SUBAGENT_START`, `SUBAGENT_END` | `subagent.started`, `subagent.completed`, `subagent.failed` plus task reconciliation. Use `tool_call_id` for the Oto widget and task registry IDs internally. |
| `BG_COMMAND_START`, `BG_COMMAND_END` | `session.background_tasks_changed` is an invalidation event with no task payload. Fetch tasks, reconcile shell status, and correlate shell task IDs with originating tool calls. Native shell metadata does not by itself establish that correlation. |
| `DELEGATE_SPAWN`, `DELEGATE_RESULT` | Oto delegation MCP tool activity; preserve existing scheduler/run identity. Copilot native subagents are a separate mechanism. |
| `WORKFLOW_START`, `WORKFLOW_PROGRESS`, `WORKFLOW_END` | Experimental `factory.run_*`/fleet APIs are candidates, not established Claude workflow equivalents. Need explicit phase-tree/settlement mapping or a platform workflow adapter. |
| `PLAN_MODE` | `session.mode_changed`, plan reads, `on_exit_plan_mode_request`; policy enforcement remains independent of displayed mode. |
| `TODO_UPDATE` | `session.todos_changed` is an invalidation event. Fetch `rpc.plan.read_sql_todos()` or dependency variant and normalize status/order. |
| `GOAL_UPDATE` | `session.autopilot_objective_changed` followed by `rpc.autopilot_objective.get_state()`. Goal budget/control gaps described below. |
| `CONTEXT_COMPACT` | `session.compaction_start`/`session.compaction_complete`; use success and available pre/post counts. Failed completion is not successful compaction. |
| `METADATA` | `assistant.usage`, session usage/context events and RPC snapshots. Deduplicate per API call, preserve account attribution, and keep missing values unknown. Copilot credits are not dollars. |
| `SYSTEM`, `ERROR` | Session start/resume/info/warning/model-change/context and error events; sanitize credential-bearing error text. Unknown events should be observable without becoming text or fabricated success. |
| `DONE` | Candidate `session.idle`, which also reports optional `aborted` and mode. Establish actual turn/follow-up behavior with recorded traces. `assistant.turn_end` alone does not prove all session work is settled. |
| `QUEUE_TURN`, `ARTIFACT_TURN`, `PRODUCER_DONE` | Oto producer events, not direct Copilot translations. Keep delivery, persistence and lifecycle responsibility with the existing producer. |

## Parity gaps requiring explicit decisions and tests

### Permissions and account identity — P01, P03, P04, P11, P15

The wheel exposes pre-tool hooks with tool name/arguments and allow/deny/ask,
argument replacement, and context output. A separate pre-MCP hook exposes server
identity and metadata. Permission callbacks support typed approval/denial
results. This is sufficient to attempt the existing authority bridge, but not to
assert complete coverage. Test shell aliases, native GitHub tools, user-requested
shell commands, plugins/file hooks, custom/native subagents, URL access and MCP.

No-handler event mode can leave permission requests pending. The SDK's internal
handler-error path denies, but that is not a substitute for an Oto timeout and
unattended policy. Re-register policy on every resume. Do not approve all or let
native remembered approvals override an Oto denial. File/config discovery,
cross-session stores, keychain use and managed settings each need an explicit
choice rather than default inheritance.

GitHub documents OAuth user tokens and fine-grained PATs; classic PATs are not
supported. Disable stored-user fallback and distinguish Oto login, inference
payer, and repository identity. The published SDK additionally exposes rotating
session token providers, but credential refresh behavior must be tested with the
chosen account type. [User authentication](https://docs.github.com/en/copilot/how-tos/copilot-sdk/auth/authenticate)

Organization service authentication has a different documented path: an eligible
GitHub App installation with Copilot Requests permission, current all-repository
installation requirement, and organization policy. Supply its one-hour token
through the runtime environment, not the SDK user-token option; the documented
refresh requires runtime restart. Do not assume the SDK user-token provider
removes this requirement. Restart/resume with background work is a release gate.
[Service authentication](https://docs.github.com/en/copilot/how-tos/copilot-sdk/auth/server-to-server-tokens)

### Goals and background work — P07, P08, P12, P14

The native objective projection exposes `active`/`completed`/`paused`, objective
text, turn count, exact credit consumption as decimal nano-AIU, optional credit
limit and pause reason. Oto's goal contract instead includes token budget,
tokens used, elapsed time, `usageLimited`/`budgetLimited`, clear and pause/resume
controls. A credit cap cannot be relabeled a token budget.

The public generated objective API inspected here offers `get_state`; workspace
methods read/write/delete a state file. Editing that file is not proof that a
live objective changes safely. Establish supported commands for objective
creation/update/pause/resume/clear, or implement a durable Oto goal supervisor
with accounting and interruption. Do not fabricate unknown token/time counters.

Task APIs cover background agents and shells, refresh, cancellation, progress,
and settlement including follow-up turns. This substantially reduces the gap,
but registry correlation and behavior after detach, abort, process death and
credential restart remain unproved. The shell API's own contract notes that
POSIX descendants escaping the process group can survive termination. Oto must
supervise sandbox descendants rather than assume runtime shutdown is exhaustive.

### Native terminal and resume — P13, P14

The native CLI documents `--resume`, while SDK lifecycle methods can query/set
the foreground session of a TUI-plus-server runtime. These are candidate handoff
paths, not evidence of safe simultaneous SDK/PTY ownership.
[CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)

Run a native terminal with the same pinned full CLI release and isolated state
root, then test SDK → PTY → SDK history, tool records and policy restoration.
The SDK's staged headless bundle does not itself prove availability of a complete
interactive frontend. Enforce an explicit writer lease; attaching a second
frontend must not bypass approval or resume stale pending actions.

`resume_session(..., continue_pending_work=False)` is the published default;
pending actions are treated as interrupted. Replaying pending work after a crash
requires reconciliation and explicit policy. Resume must re-supply hooks, custom
tools and startup-only policy. `disconnect()` preserves history but detaches
callbacks; test whether active work remains and who owns pending approvals.
Separate cold history restoration from adoption of a live remote runtime.

### Remaining product surfaces — P02, P05, P06, P09, P10, P16

Dynamic model/effort discovery, BYOK/provider options, MCP, skills, images and
configuration controls exist in the wheel. Oto still needs account/settings UI,
runtime packaging, config/path translation, brokered secrets, artifact delivery,
spawn-path audits and validation across dashboard, remote workers, phone and
meetings. Extension options can have different cold-resume versus resident-resume
semantics: changing a setting in the database does not guarantee it takes effect
in an already-running runtime.

## C1 exit evidence still required

1. Bounded sandbox startup/teardown and pinned runtime verification on Linux;
   identify the remaining platform matrix explicitly.
2. Recorded authenticated turns covering tool denial, MCP, questions, models,
   attachments, usage and token expiry under the selected identities.
3. Recorded control races: steer/abort/stop-and-send, compaction, background
   completion after idle, and objective changes.
4. Native terminal/headless round trip with restored policy and one writer.
5. Crash/reconnect/refresh tests that distinguish interrupted work, continuing
   work, and uncertain external effects.

This inventory completes source discovery only. None of P01–P16 is accepted on
the basis of this document alone.

## Offline translator foundation

[The initial translator](../../proxy/core/layers/copilot/translator.py) accepts
SDK `SessionEvent.to_dict()` output and implements ordinary text/reasoning/tool
display, errors, and an explicitly reconciled idle candidate. It is not registered
as an execution layer. Unknown SDK enum values retain their actual wire names in
`raw_type`; `to_dict()` restores that name. Do not record only `event.type.value`,
which collapses those events to `unknown`.

[The isolated test suite](../../scripts/copilot/tests/test_translator.py) covers
synthetic reordered/duplicate frames and idle races without installing the SDK
or starting PostgreSQL. These tests establish adapter behavior, not runtime
compatibility. Chunked final-message snapshots fail explicitly until their
streaming relationship is verified. Background settlement requires a future
supervisor's affirmative reconciliation; native `session.idle` never emits DONE
by itself. Other unmapped top-level events remain observable once per type per
session without forwarding their raw payloads. Repeated types still invalidate
a pending idle reconciliation; limiting diagnostics does not erase activity.
The SDK's `assistant.streaming_delta` contains cumulative response-byte progress,
and `assistant.tool_call_delta` contains partial tool arguments. Both are quiet:
text/reasoning deltas and the complete arguments in `tool.execution_start` provide
the corresponding user-visible content. This avoids a SYSTEM event for every
token or partial tool-argument fragment.
