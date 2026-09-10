# Copilot owned-session lifecycle registry

This slice makes explicitly created local Copilot sessions visible to shared
lifecycle consumers. It does not register Copilot in the engine factory or
enable generic dashboard, scheduler, phone, satellite, prewarm or model-picker
configuration. Account selection and the supported native-tool profile remain
the explicit caller's responsibility.

## Ownership and usability are different facts

A session can own startup work, processes, callbacks or a durable writer claim
while it is unavailable for another turn. Shared lifecycle code needs both
facts:

| Query | Meaning | Appropriate consumers |
| --- | --- | --- |
| Owned membership | This generation still owns resources or has not proved clean disposal | Concurrency, destructive retention, stop, shutdown, engine changes |
| Active/usable | This generation currently accepts ordinary session work | Liveness display and the applicable session-token confinement check |
| Persisted security context | Permission-routing identity retained for a session | Permission checks; never sufficient proof of runtime ownership |
| Session tracking metadata | Agent, recency and history-index information | Display/history lookup; never sufficient proof of runtime ownership |

Publish ownership synchronously before the first startup await. Startup,
closing and failed-cleanup entries remain owned even when active is false.
Remove an entry only after the same generation completes clean disposal.
Membership protects resources after the security context or tracking metadata
has disappeared; registry snapshots must therefore retain their own captured
agent/session metadata.

The registry is process-local. It does not replace durable Copilot session
records, their writer lock, the runtime's exact process ownership, callback
draining, shell fences or credential-lease validation. A registry snapshot does
not prove that a native turn is settled, that a process has exited, or that
history is eligible for resume.

The Copilot layer's reaper watches owner closure; it is not an idle-timeout
sweeper. Registry membership supplies no activity policy. The explicit caller
still owns ordinary close, with credential/context revocation and runtime-health
monitoring as backstops. Automatic idle cleanup requires separate qualification
before broad engine registration.

`core/session/owned_sessions.py` exposes immutable-identity `OwnedSession`
handles with captured `session_id`, `engine`, `agent`, `user_sub`, mount
`username` and `local` metadata. `register_owned_session` validates and publishes
one handle synchronously. `get_owned_session`, `owned_sessions` and
`owned_session_ids` return ownership without consulting the active callback;
the snapshot helpers can restrict results to local owners.
`release_owned_session` accepts only the original currently registered handle.
`begin_owned_session_shutdown` seals admission before returning a snapshot; it
does not reopen admission in that process or stop resources by itself.

## Generation-safe close requirements

A lifecycle sweep captures a generation, not merely a session ID. Closing an
old snapshot after that ID has been reused must never dispatch close to the
replacement. Close callbacks must capture the owned entry and identity-check
it; resolving the agent's current execution engine and calling
`close_session(session_id)` later is insufficient.

Concurrent close requests converge on the same owner's cleanup. A cancelled
consumer does not authorize abandoning cleanup or removing its claim. An
exception during cleanup leaves an owned, unavailable tombstone. A failed
active-health callback must not make membership disappear or discard the
captured metadata. Conversely, retained membership must not keep an unavailable
session's token active.

`OwnedSession.active` additionally requires current handle identity and no
registry close task. Its callback must return exactly `True`; exceptions and
other values fail closed. Identity and close state are checked again after the
callback returns, so a callback that releases/replaces its own claim cannot
make the old generation active. Copilot's `_live` also requires `claim.active`,
closing the interval between a registry close request and layer cleanup startup.
`OwnedSession.close` returns false for a stale
snapshot, shares one close task across current callers and shields that task
from waiter cancellation. A cancelled waiter may return before cleanup finishes;
the registry retains ownership until the actual owner releases its handle.
These methods do not automatically choose or instantiate an execution layer.
If the callback returns while its original claim remains registered, close
fails with a sanitized cleanup error rather than reporting successful disposal.

The Copilot layer still owns platform context cleanup by exact object identity.
A foreign replacement context must survive disposal of the old generation.
Publishing a starting owner must also not make its own subsequent startup
check mistake that entry for foreign ownership.
Copilot uses `session_manager.has_legacy_session` for admission and its final
pre-registration recheck, separately from the active-status query. Thus a
starting Copilot handle cannot mask a same-ID legacy pool entry.

After successfully joining runtime cleanup and clearing its own exact platform
context, Copilot immediately calls `release_chat_slot`. Cleanup failure retains
the budget; a foreign replacement context also prevents this old generation
from releasing that session ID's slot. Successful disposal removes
the layer entry, cross-instance claim and registry handle.

## Security-context persistence and token limits

`core/session/session_state.py:load_session_security` reloads recent security
contexts at startup because remote sessions can survive a proxy restart. It
excludes external principals and expires old contexts, but this is not a live
runtime inventory. `cleanup_session_permission_state` denies outstanding
platform waiters, removes owned security/mode state and clears liveness. None
of those operations substitutes for joining Copilot runtime resources.

`middleware.py:external_session_confinement` consults
`session_manager.is_session_registered` only when a session JWT carries an
`ext` claim. Its active check must fail for starting, closing and failed owners.
Ordinary session JWT decoding in `auth/session_token.py` still validates
signature, type and expiry; this registry change does not claim universal token
revocation or generation-bound JWTs. Copilot's current local human-session
adapter does not enable external-session routes.

`prune_dead_sessions` and `reap_task_sessions` prune the separate tracking
index by age. They neither stop processes nor prove their death. A resource
snapshot must remain useful after that index entry is removed. Copilot task
execution remains unsupported by the current adapter.

## Shared call-site requirements and limits

Integrated shared consumers consult captured owned metadata and
close the captured generation, without deriving the owner from mutable agent
configuration:

- `core/concurrency.py`: count and protect owned sessions, including startup
  and failed cleanup; avoid evicting a replacement from a stale candidate.
- `services/scheduler/scheduler.py`: distinguish owned resources from an orphan
  when inspecting existing session IDs. After its lane-quiescence ceiling, it
  re-reads the current pump and refuses to reap if an owner still exists; elapsed
  time is not cleanup proof. This does not enable Copilot tasks.
- `api/agents/chats.py`: stop owned resources before destructive chat cleanup;
  close failure or any remaining/replacement claim yields a static HTTP 503
  and preserves the chat row. Legacy close remains best-effort. This does not
  roll back continuation cancellation already performed before the close.
- `services/infra/retention.py`: protect session IDs and agent resources even
  after their permission contexts or tracking entries disappear.
- `startup.py`: seal owned admission and capture generations before the first
  shutdown await. Retain the existing scheduler/meeting drain first, then
  concurrently dispatch every local captured close with isolated failures.
  One slow owner cannot prevent the other captured owners from starting cleanup.
  The outer shutdown deadline still applies, and dependency draining can use
  that deadline before owned closures are dispatched. Remote shutdown policy
  remains unchanged; this is not a new periodic idle sweep.
- `ws/dashboard.py`: `chat_process_alive` conservatively treats membership as
  alive because its callers protect stop/engine-switch decisions. This helper
  does not promise that the session can accept another turn.

Two existing facilities require separate qualification before future Copilot
integration. `prewarm_session_registry.reap_stale` currently resolves mutable
agent/target configuration before closing by session ID; a future Copilot
prewarm path needs an exact-generation handle. Its current generic warmup
configuration is not admitted by the Copilot adapter.
`services/engines/subscription_pool._session_registered_live` still inventories
the generic subscription consumers. Copilot has an independent typed account
lease and rejects generic subscription bindings; adding it to that pool would
not establish correct payer or refresh semantics.

This document records the reviewed lifecycle requirements and implementation
interfaces. It supplies no independent inference or token-entitlement evidence;
verification results belong to the phase's test and probe report. The final
implementation check reported 135 focused tests (31 registry, 26 concurrency,
12 shared consumers and 66 layer tests), plus an offline run of 1,043 tests and
153 subtests. The three-turn live probe subsequently passed in 37.075 seconds;
see the [results and limits](copilot-session-registry-results.md).
