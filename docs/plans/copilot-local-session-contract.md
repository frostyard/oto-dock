# Copilot local account/session composition contract

This contract describes the implemented `CopilotLocalSession` composition and
its remaining integration boundaries. It does not register a
Copilot execution layer or claim the remaining parity work is complete. The
source audit itself made no inference calls; separate live factory probes
exercise authenticated inference and resume.

## Separate identities and authority

The owner must keep these identities distinct:

| Identity | Source and purpose |
| --- | --- |
| Platform session ID | Host-selected OtoDock session key; permission/question routing and live session ownership |
| Agent name | Agent/config authorization and sandbox workspace selection |
| Driving user | Authenticated caller identity, distinct from payer selection |
| `CopilotAccountScope` | Explicit personal user or platform payer scope |
| Account ID, GitHub principal, revision | Exact encrypted credential generation leased for this runtime |
| Native SDK session ID | Native conversation identity bound before any SDK startup callbacks |
| Private state allocation/profile | Host-owned history and the exact guarded configuration under which it was created |
| Runtime/process generation | Single owned sandbox process lifetime; never inferred from a namespace-local task PID |

`AgentConfig.user_sub` identifies the user driving a session. Existing
`subscription_user_sub` separately records subscription acquisition scope;
neither should be silently repurposed as a Copilot account selector. A factory
must receive an explicit account ID and typed scope from an authorized host
caller. `CopilotAccountScope.personal(user_sub)` requires a nonempty identity;
`platform()` requires `user_sub=None`. An empty string is not platform consent.

`storage.copilot_account_store.read_credential(account_id, scope)` requires an
active account. Personal scope requires its owner to match the scope user and
`use_personal=True`. Platform scope requires `contribute_platform=True` and the
owner's current platform role to be admin. This checks account eligibility; it
does not authorize a caller to start an agent or choose platform billing.
Those decisions remain with the authenticated session/config entry point.

## Live platform context must precede spawn

The concrete platform context is `auth.path_policy.SecurityContext`, a frozen
dataclass with agent, effective role, mount username, target metadata, visibility
and external-caller fields. The factory requires a local target and matching
agent/sandbox fields, admits only user/agent principal and session scopes, and
rejects external sandbox/home and arbitrary `work_cwd` inputs. It does not
implement a complete client-type allowlist or validate every otherwise unused
remote metadata field. Those are future authenticated entry-point qualification
requirements, not guarantees of this primitive. The trusted caller must not
construct an admin context or substitute a permissive fallback when context
is absent.

Existing local layers call
`register_session_state(platform_id, permission_mode, security_context)` before
process startup, and `cleanup_session_permission_state(platform_id)` on failed
spawn or final close. Registration sets mode and persists security context.
`set_session_security` may replace the dataclass to stamp `cli_session_id` for
a UUID-shaped platform ID. Therefore any identity check must pin the actual
`get_session_security(platform_id)` value after registration, not require that
it be the same Python object originally supplied by the caller.

Registration is not an exclusive claim: it can overwrite existing state. The
owner must reject a duplicate live platform session before touching its context
and remove only registrations it owns during cleanup. In particular, failure
cleanup for a rejected duplicate must not erase another running session's
permission state.

`bind_platform_authority(platform_id, supervisor.requests,
working_directory=builder.get_cwd(), expected_sdk_session_id=native_id)` connects
the native bridge to the real `decide_tool_permission` and `ask_user_question`
functions. It snapshots context, mode, client type and meeting route before a
held answer and refuses the answer if that context changes while waiting.
With no owner callback its legacy context predicate only requires a non-None
registered context. The new optional `owner_valid` callback must return exactly
True when supplied. The factory always supplies its own predicate, checking
the pinned context object's identity and copied value, owner lifetime and lease
validity. A missing, false or throwing owner result denies; held human answers
are rechecked before return. Current context/mode must also govern
each tool authorization; a credential lease does not provide path authority.

The independent half-second owner observer closes an opened owner when its
registered context is removed, replaced or changed, or when runtime health,
lease validity or sticky provider/supervisor failure checks fail, including
when no consumer is reading events. Startup and each submission recheck around
awaited authorization; monitoring is not a substitute for those checks.

Permission/question cleanup is concrete, not just dropping a bridge reference.
`cleanup_session_permission_state` denies unresolved platform permission waiters,
clears liveness, modes, emitters, security registration, meeting routing and
credential-broker state. The supervisor additionally owns native request
inventories and callback cancellation/join. Preserve both cleanup paths.

## Required local composition

`CopilotLocalSession.open(config, builder=..., runtime_path=..., records=...,
resume=False)` returns one owner exposing stream, steer, abort, interrupt and
idempotent close. It intentionally exposes no raw SDK session. Final state
discard is outside this API. `CopilotLocalSessionConfig` carries the platform
session ID, selected account/scope, driving user, model, explicit tool subset
and optional system prompt. The implemented construction sequence is:

1. The trusted caller authenticates agent access and registers the platform
   context before `open`; it retains ownership of routing/permission cleanup.
   The factory validates the live concrete context, local target, explicit
   payer scope, model and guarded tool subset. Reject unknown
   or protected SDK options rather than merging caller dictionaries over policy.
2. The caller resolves a real sandbox with `resolve_sandbox_config` and supplies
   an exact `SandboxBuilder`. The factory clones its configuration and checks
   the live context's role, mount username, agent,
   high-clearance flag, visibility, shared mount, knowledge-write settings and
   attached knowledge libraries against the supplied configuration.
   Preserve host-vs-sandbox path separation: runtime transport uses an admitted
   host cwd; the permission bridge uses `builder.get_cwd()`.
3. Acquire `CopilotLeaseGuard` for the exact selected account and scope; never
   choose a substitute account on failure. Create or exclusively reopen the
   durable profile/state record before starting a runtime.
4. Construct `SandboxedCopilotRuntime` with the typed leased credential, explicit
   environment, pinned read-only runtime assets and private session state.
   The state mount is fixed at `/var/lib/otodock/copilot`. Runtime startup is
   mandatory sandboxed; do not fall back to a direct SDK process.
5. Create `CopilotSessionSupervisor` and bind its submission authorization to
   live context validation plus `lease.authorize()`. Connect independent lease
   invalidation to `supervisor.invalidate_credentials()` so revoked accounts
   close work even while an event consumer is paused. Handle revocation during
   partial startup before the supervisor/backend exists as well.
6. Start the runtime and immediately call `capture_process_fence()` before any
   SDK session or tool activity. Install the real platform bridge and a fresh
   `CopilotNativeToolPolicy`; its guarded create/resume checks the actual catalog
   and binds native identity before startup callbacks. The factory checks native
   authentication and exact model availability and revalidates the lease before
   opening the session. Feed `on_event` through the factory's `_receive_event`
   wrapper from session creation onward. It records native `session.error` as
   sticky uncertainty/provider failure, replaces its payload with a static
   message, and forwards the frame to `supervisor.receive_event`. Malformed
   conversion is forwarded as an invalid frame to trigger supervisor failure;
   it is not thrown back as an SDK callback exception that could be swallowed.
7. Wrap the returned native session in `CopilotNativeShellSession`, injecting
   `process_fence.is_settled`, and bind that backend exactly once to the
   supervisor. Revalidate authorization again and start the independent context
   observer before publishing the usable owner to callers.

Open has a 60-second outer deadline. It also checks the monotonic deadline and
the calling task's cancellation count after awaited acquisition, so a dependency
that suppresses cancellation and returns a late resource cannot publish a usable
owner. Runtime startup and native policy RPCs have their own bounds. The factory supplies an explicit minimal environment and a
30-credit session limit; selected model availability and token authentication
do not prove entitlement for every future model call. The default turn deadline
is 300 seconds, with an accepted caller range up to 1800 seconds.

`resolve_sandbox_config` is the existing egress/mount authority. It resolves
network isolation and knowledge/MCP mounts; `SandboxBuilder` rejects an empty
egress-forward configuration. A test-only hand-built sandbox with artificial
forward ports is not the production configuration path. Runtime asset mounts
must be explicitly admitted read-only. The native-only profile should not
import agent MCP configuration or broad credential environments merely because
other engines do so. The default builder mount behavior must still agree with
the intended visibility/context; a safe inference token channel cannot correct
an overbroad workspace mount.

The supervisor owns a single event producer, writer serialization, request and
callback registries, bounded queues, deadlines and event/snapshot settlement.
Its submission callback runs inside the writer lock. The lease separately
polls authoritative encrypted storage, pins the entire credential generation,
and closes on expiry, replacement or eligibility loss. It is process-local;
it is not a cross-process writer lease or a token refresh implementation.
Unknown user-token expiry remains unverified and cannot satisfy a positive
minimum runway requirement.

## Private history and profile provenance

`PrivateCopilotSessionState.create(trusted_root)` allocates a unique 0700
directory using a pinned directory FD. The root must be outside agent-writable
trees and under exclusive host control. Two sessions for the same user, agent
or account still receive distinct allocations. The helper validates pathname
ownership/identity on access and only discards its own allocation.

A profile record must bind history to the host owner, agent, explicit scope,
account/principal, native session ID, state allocation and guarded native-tool
profile/version. Resume verifies this record against the intended ownership and
current lease before starting a new runtime. Credential revision changes always
require a fresh runtime; the implemented rule permits a newly eligible revision
only while the exact account/principal/profile remain unchanged. It never
switches accounts automatically. Tool configuration and account boundaries cannot be reconstructed
from a model-provided path or conversation text.

The SDK omits empty `tools` and `mcp_servers` on resume. Empty Python values do
not clear persisted custom tools or MCP configuration. Only history originally
created under the same guarded profile is eligible. Resume disables automatic
pending-work continuation and must never fall back to creating a fresh native
session when the recorded conversation is absent.

The implemented `CopilotSessionRecords(root, state_root=...)` uses separate
trusted 0700 roots, a nonblocking per-platform-session `flock`, private record
files and exact profile matching. `create(profile, native_session_id=...)`
creates an ACTIVE record; `open(profile)` admits only READY records and marks
them ACTIVE before handing state to a runtime. A free lock on an ACTIVE record
does not make crash recovery safe and is refused. No failed resume falls back
to new history.

`PrivateCopilotSessionState` now supports validated allocation records,
`reopen(root, allocation)` and `detach()`. Reopen verifies recorded root/state
device and inode identities, allocation name and private ownership. Detach
closes the host handle while retaining history; it is distinct from discard.
The factory hashes sandbox/context/system-prompt/runtime-path/cwd configuration
into the profile. The profile also fixes SDK/runtime/policy versions, tool
subset, model and owner/account/principal/scope. Credential revision is leased
per runtime rather than persisted as history ownership: a fresh eligible
credential for the same exact profile can be acquired on verified reopen.
These mechanisms support clean-close reopening; they do not automatically
recover a runtime that crashed while ACTIVE. No token is stored in the record
or allocation metadata.

Permission mode, client type and meeting routing are deliberately not part of
the durable profile. A resumed owner binds their current platform values and
the permission bridge checks them around each held decision. They may therefore
change between clean openings without changing the stored profile. The pinned
`SecurityContext`, builder configuration, account/scope/principal, model, tool
subset and system prompt still require exact provenance matching. This is
fresh policy binding on resume, not replay of an old approval or queue route.

## Closing and failure ownership

Close admissions first, invalidate held decisions, cancel/join host callbacks
and requests, disconnect the SDK session, and close the exact owned runtime.
Stop the lease observer and release the exclusive record handle. The caller
then releases its platform permission/routing state and any acquired chat slot;
the factory does not own or erase those shared registrations. Every partial-startup failure and caller cancellation
must converge on the same idempotent cleanup. Do not make lease cleanup call
supervisor cleanup recursively; one owner coordinates their lifetimes.

The factory marks a record READY only after a completed, fully drained stream,
normal runtime cleanup, no in-flight stream, no uncertain control/failure or
revocation, and unchanged live context. Abort and interrupt deliberately leave
the record ineligible for resume because history preservation remains a
separate unqualified contract. Failed/uncertain startup remains ACTIVE and is
not silently reused.

Native provider errors and supervisor failures are sticky: a later `DONE` does
not restore READY eligibility. The factory tracks error events while streaming,
checks runtime health after startup, and sanitizes stream/control exceptions.
This state survives an otherwise successful subsequent native event; closing
cannot relabel a failed owner as clean history.

Runtime close does not discard private history. Preserve a valid allocation for
an explicitly permitted resume; final discard is separate and must happen only
after every runtime using that state is stopped. If process cleanup is uncertain,
do not delete a directory still mounted by possible live work or report clean
completion. Sanitized failure reporting must omit credential-bearing exception
payloads and profile secrets.

## Eventual ExecutionLayer registration is a separate gate

The existing ABC consumes `AgentConfig` in `start_session`, streams `CommonEvent`
from `send_message`, and requires close, permission response, mode/model/control
methods and a capability descriptor. Several semantics prevent a direct wrapper
around supervisor acknowledgements:

| Existing layer contract | Copilot integration constraint |
| --- | --- |
| `abort()` returns true for graceful native history preservation | Accepted control and joined callbacks alone do not prove that history contract |
| `steer()` returns true when accepted exactly once | Preserve pending/delivered input ownership; never also enqueue an accepted message |
| `interrupt_for_queued()` preserves background work | The qualified Copilot shell adapter cancels its owned shells on accepted interrupt; it is not an equivalent implementation |
| `can_resume_session()`/`prepare_resume()` | Require verified persisted conversation/profile/state, not just a remembered ID |
| `drain_bg_commands()` and subagent registries | Qualified attached-shell settlement does not implement model-facing background controls or subagent parity |
| `is_session_alive()`/process-death probes/session locks | Must reflect the concrete owned runtime and session registry, not persisted security state |

`session_manager.register_layer()` alone is insufficient. Unknown execution
paths currently fall back to Claude, and `is_session_registered()` explicitly
checks the Claude, Codex, Direct and remote session pools. That function is used
by session-scoped credential confinement; a future Copilot registry must be
integrated there before issuing those capabilities. Config builders, account
selection, scheduler/client-type routing, capability/model metadata, idle reaping,
chat-slot release and shutdown sweeps also require explicit integration.

Keep this factory unregistered while those contracts remain incomplete. A local
owner with real account and path authority is useful progress, but does not by
itself enable organization agents, unattended workflows, MCPs, PTYs or full
Codex/Claude parity.
