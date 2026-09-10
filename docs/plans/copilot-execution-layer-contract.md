# Bounded Copilot ExecutionLayer contract

This slice composes the qualified local factory behind the existing
`ExecutionLayer` interface. The adapter remains unregistered: no engine picker,
generic config builder, scheduler, satellite or phone route is enabled by adding
the class. This document records reviewed call-site requirements and the agreed
implementation boundary for `CopilotExecutionLayer`. The bounded adapter has
offline tests and a three-turn live probe; those checks do not qualify the
unimplemented generic entry points listed below.

## Explicit caller configuration

`CopilotAgentConfig(AgentConfig)` adds explicit `account_id`,
`account_scope` and `enabled_tools`. The authenticated caller must authorize
agent access and payer selection before constructing it. A personal account
scope must match the driving user; platform scope must be a deliberate choice.
Account eligibility is revalidated by the real Copilot lease/store, never by
reading token-shaped strings from a generic environment.

The initial adapter admits human dashboard/SSE local sessions with a concrete,
matching `SecurityContext`. It rejects generic credential/extra environments,
MCP config and secret bundles, generic subscription acquisition, interactive
PTY options, remote execution and provider-specific extras. Fields inherited
from `AgentConfig` are not automatic capabilities. The model and exact supported
native tool subset remain explicit. The caller supplies runtime and private
record roots to the layer constructor; no executable or history directory is
selected from a model message. The concrete constructor is
`CopilotExecutionLayer(runtime_path=..., records=CopilotSessionRecords(...),
homes=CopilotSandboxHomes(...))`; record, state and scratch-home roots must be
separate and nonoverlapping.

Sandbox construction uses `resolve_sandbox_config` and the real
`SandboxBuilder`, carrying the live context's mount username, role, agent,
visibility and knowledge settings. A dedicated scratch configuration home under
a separate private host root supplies the builder's required configuration-home
argument; it must not adopt a caller's existing `.claude` or `.codex` credential
directory. Runtime assets are explicitly mounted read-only. Private records and
state roots remain outside all ordinary agent mounts, with only the selected
state allocation mounted by the runtime owner. `CopilotSandboxHomes.get` uses
a hashed platform-session child under a pinned private root. The layer inspects
the generated bind arguments and refuses any mount exposing that root, an
ancestor, or another child, including read-only binds; only the exact selected
home is eligible. The helper retains scratch contents on close rather than
copying another engine's credentials or deleting history implicitly.

The resolver's internal `trusted_runtime_mounts` channel accepts only explicit
`SandboxMount` objects with mode `ro`, existing absolute directory sources and
normalized absolute destinations outside protected sandbox paths. Invalid
entries fail startup. This allows the trusted caller to stage runtime assets
outside the agent/MCP trees without widening the MCP manifest source allowlist.
Later conditional MCP mounts cannot overlap a trusted asset destination or its
ancestors/descendants. Linux double-leading-slash destination aliases undergo
the same protected-path and overlap checks.

The adapter explicitly enables `isolated_config_home`. The builder validates
the selected `host_claude_dir` through a non-symlink directory walk, requires
current-user ownership and mode 0700, then adds it as the final workspace mount
at the selected cwd's `.claude` path. A missing literal mountpoint is created
before a read-only user-root bind. This shadows that session's legacy `.claude`
directory; it does not remove other engine credential paths from ordinary
workspace mounts. Tool authority remains required. The default is false, so
other engines retain their existing mount behavior.

Both new `SandboxConfig` fields participate in the factory's complete config
digest, including their defaults. Previously created preview history therefore
does not silently resume under the changed sandbox profile.

The new mount suite covers 64 cases, and the existing viewer/editor/manager,
agent, MCP and namespace builder classes pass 27 cases. The actual resolver and
adapter live probe completed three bounded turns with normal cleanup in
63.851 seconds; see [the retained evidence](evidence/copilot-execution-layer.json).

## Session ownership and lock separation

Reserve each platform session ID across adapter instances before registering
permission state or awaiting slow startup. An instance-local dictionary alone
would allow two layers to overwrite the same global security/permission route.
The reservation must reject a duplicate without cleaning up the existing owner's
state. Durable session records provide another exclusive writer boundary; they
do not replace the pre-registration platform reservation.

The module-level `_claims` map is reserved synchronously before scheduling
startup. `_start` then rechecks foreign security and live-session registration
immediately before installing context, without an intervening await. This
second check prevents overwriting another engine's context created while the
startup task was waiting to run.

The layer calls `register_session_state` before opening the factory and pins
the actual registered context after any UUID stamping. The layer owns that
registration because the underlying `CopilotLocalSession` deliberately does
not. Final cleanup may remove it only if the registered context is still the
one this layer installed. Deny/release pending platform permissions/questions
and clean up liveness along with the owned registration. Record history is
retained according to the factory's ACTIVE/READY rules, not deleted at every
layer close.

There are two different locks:

- A **producer lock** serializes complete streamed turns and, for some callers,
  multi-turn queue processing. `session_lock()` exposes this lock.
- The supervisor's **writer lock** protects mutation dispatch and ACK handling.
  Abort/steer admission must not wait behind the producer for the duration of
  inference. Never expose the writer lock as `session_lock()`.

Concrete callers include `core/events/task_producer.py:138` and `:357`,
`ws/dashboard_chat.py:2725`, `services/meetings/meeting_orchestrator.py:325`, and
`ws/duplex_attach.py:475`. The task producer releases its lock between sends
before background drains; other producers can hold it across a queue loop.
The important common constraint is that the lock spans streamed event yields.
The adapter also carries the locked entry identity in a context variable.
`send_message` refuses a producer inherited from a different or replaced entry,
so an old queue-drain task cannot accidentally submit to a new owner that later
reuses the same platform ID.

The ABC's `get_session()` has no additional production call sites requiring a
Codex/Claude internal object shape. This adapter returns its owned
`CopilotLocalSession`, which does not expose the raw SDK. Health checks
must describe the actual owner. A live root and confirmed complete process
cleanup are separate facts: `probe_session_process_dead()` must not authorize
irreversible reap merely because cleanup started or observation failed.

An owner can fail independently while the consumer is idle. The layer's close
reaper therefore waits for the owner's closure notification, releases only its
own context and reservation, and does not require another client request to
discover revocation or runtime death. Shutdown and failed startup converge on
the same idempotent cleanup path.

The `_watch` task schedules cleanup after `owner.wait_closed()` but does not
await its own cleanup task; cleanup cancels/joins the watcher without a cycle.
Only successful cleanup removes the registry entry and cross-instance claim.
Failed cleanup retains a closing tombstone and blocks reuse. Consequently
`is_session_process_dead()` remains false during startup, cleanup or failed
cleanup, even though `is_session_alive()` is false. Absence from the global
Copilot claims map represents a never-owned or successfully cleaned-up ID; it
is the only true case, including queries through another adapter instance.

## Deliberately conservative ABC behavior

| Method | Initial adapter behavior and reason |
| --- | --- |
| `start_session` | Open the explicit local Copilot config; reject duplicate, unsupported or ambiguous configuration |
| `send_message` | Stream the factory's `CommonEvent` output; preserve consumer cancellation and cleanup |
| `abort` | Close the owned runtime and return false; no graceful-history claim |
| `steer` | Return false without dispatching anything until accepted-message/delivery semantics are qualified at the layer boundary |
| `interrupt_for_queued` | Return false; the existing contract preserves background work, whereas the qualified Copilot interrupt adapter cancels owned shells |
| Model/mode changes | Only an identical-value no-op; reject changes until profile, context and UI semantics are qualified |
| Permission response | Resolve only an existing request bound to this platform session |
| `compact` | Unsupported default; do not advertise context compression |
| `can_resume_session` | Conservatively false without the full authorized config/profile; an agent name and username cannot authorize a durable account-bound resume |
| Explicit `config.resume` | May reach the factory's exact profile/account/READY-record verification; no missing-history fallback |
| `prepare_resume` | Dispose of only the existing owned session; never clear another owner's registration or upgrade an ACTIVE record to READY |

The capability descriptor advertises permissions but not MCPs, resume, plan
workflow, steering controls, subagents or compression. The explicit permission
mode vocabulary is `default`, `acceptEdits`, `plan`, `dontAsk`; admitting a plan
permission floor is not a complete plan-workflow claim. Model/mode no-ops require
the configured value, and mode additionally must equal current registered mode.
`send_message` initially accepts only absent options or `inject_time=False`.
Generic dashboard queued sends currently use `inject_time=True` at
`ws/dashboard_chat.py:1697` and `:1711`; those and image/attachment options need
separate qualification before wiring this adapter into that flow.

This distinction is required by the existing callers. In
`ws/dashboard_dispatch.py:332`, `ws/dashboard_chat.py:2051` and
`ws/duplex_attach.py:608`, true from `abort()` keeps the pump running and suppresses
cancelled-context injection. False cancels the producer/pump and permits the
existing context-reseed path. A native accepted-control ACK is insufficient for
the true branch.

`ws/dashboard_chat.py:1940` and `core/session/session_delivery.py:295` persist or
deliver an accepted steer exactly once; false falls through to enqueue. An
adapter must not dispatch input and then return false after an ambiguous ACK.
The initial no-dispatch false behavior avoids that uncertainty.

Resume callers perform `can_resume_session` **before** `prepare_resume`.
`ws/headless_resume.py:113` and `ws/dashboard_warmup.py:1432` allocate a new
platform session ID when the former returns false. The explicit factory resume
path does not make this generic automatic recovery path work yet. Capability
flags must reflect that limitation rather than advertising the successful
lower-level clean-close resume probe as complete layer recovery.

Permission ownership can use
`session_state.get_permission_request_session(request_id)` before
`resolve_permission`, following `api/hooks/hooks.py:3007`. A guessed request ID
must not answer another session's prompt. The existing Codex layer's bare
`resolve_permission` call is not a reason to omit this boundary in a new adapter.

## Why global registration is still blocked

The remaining work is concrete and spans more than adding an entry to `_LAYERS`:

| Call site | Current behavior and required integration |
| --- | --- |
| `core/session/session_manager.py:50` | `is_session_registered` hardcodes CLI/Codex/Direct/remote pools; session-scoped credential confinement would not recognize Copilot |
| `core/session/session_manager.py:76` and `:191` | Unknown execution paths fall back to Claude; registering only selected lookup paths can route a persisted Copilot choice to the wrong engine |
| `core/config/config_builder.py:296`, `:550`, `:572` | Builds MCP configuration/prompts, `.claude` state for non-Codex paths and generic subscription environments; needs explicit Copilot configuration/account selection |
| `core/config/task_config_builder.py:534` and `:552` | Similar generic state/subscription path plus user-vs-agent task identity; not qualified for this human-only adapter |
| `core/config/phone_config_builder.py:242` and `:343` | Phone routes have their own identity/payer fallback semantics; not admitted here |
| `services/engines/subscription_pool.py:1495` | `resolve_subscription_env` sends an unknown layer through its generic provider environment mapping; it does not acquire a typed Copilot lease |
| `core/concurrency.py:518`, `:532`, `:671` | LRU and reservation reconciliation scan hardcoded pools; an idle Copilot runtime could lose its reservation once no active pump protects it |
| `services/scheduler/scheduler.py:212` | A pump absent from the known pools is classified as orphaned; a healthy Copilot producer could be reaped |
| `ws/dashboard.py:476` | Registry-specific liveness must recognize the new layer |
| `api/agents/chats.py:813` | Chat deletion chooses the closing layer from registry membership and otherwise returns without closing anything |
| `services/infra/retention.py:116` | Live-session and busy-home snapshots omit Copilot; persisted security state alone is intentionally not a live signal |
| `startup.py:316`, `:326`, `:763` | Reapers and shutdown enumerate current layers explicitly; Copilot needs equivalent owned cleanup |
| `api/agents/agents.py:168`, `:274` and `api/admin/execution_layers.py:111` | Engine allowlists exclude Copilot; widening them also requires qualified configuration, model and capability metadata |

The generic subscription builder also has phone-specific platform-pool fallback
and scope-sticky account behavior. Reusing its `subscription_id` or environment
as an implicit Copilot selection would bypass the explicit account/scope
contract. Account setup UI alone is not an authorized chat account-selection
path.

The bounded PR12 surface is therefore a concrete, independently testable local
adapter, explicit config and per-session ownership registry, real sandbox/factory
composition, correct cleanup and conservative unsupported methods. Global
registration, automatic account selection/resume, UI exposure, background
controls, remote/unattended routing and full Codex/Claude parity remain separate
integration gates.
