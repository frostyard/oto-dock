# Copilot shared lifecycle registry results

OtoDock's shared lifecycle consumers can now track explicitly owned runtimes
without adding the engine to the global selector. Copilot publishes one immutable
handle before startup's first await and releases it only after successful
cleanup. Legacy engine pools keep their existing behavior.

## Resulting behavior

Resource ownership and request admission are separate. Startup, closing and
failed-cleanup handles protect reservations, runtime identity and mounted homes.
Only an active handle permits the existing external-session confinement check.
Persisted session metadata or a security context alone is not runtime ownership;
this is not universal JWT revocation or a distributed writer lock.

Close callbacks capture the exact owner generation. Concurrent callers join one
shielded cleanup task, cancelling a waiter cannot cancel cleanup, and stale
snapshots cannot close a replacement. Callback return with the original claim
still retained is an error. Failed cleanup keeps an inactive blocking claim.
Copilot stops admitting new layer requests as soon as registry close begins.
Successful cleanup releases its chat reservation while it still owns the
registered route; replacement contexts and failed cleanup retain reservations.

Shared consumers now use that ownership:

- Reservation reconciliation preserves local owned sessions, including startup
  and failed cleanup. Capacity eviction excludes them because generic Copilot
  resume is not yet qualified, and rechecks ownership before removing a stale
  legacy eviction candidate.
- Scheduler lane checks do not classify owned sessions as orphaned. Reaching
  the quiescence wait ceiling does not authorize killing them; the new run fails
  explicitly while the prior owner remains.
- Engine switching remains blocked while a runtime owns resources. Chat
  deletion closes the captured owner and preserves the chat with a sanitized
  503 response if cleanup fails or a replacement claim remains.
- Retention protects captured local mount metadata even after permission
  context removal. Shutdown seals new owned-session admission before its first
  await and dispatches local closes concurrently after the existing dependent
  task/meeting drain. Existing remote in-flight preservation stays intact.

## Evidence

The [three-turn live sandbox probe](evidence/copilot-session-registry.json)
passed in **37.075 seconds** with SDK **1.0.13** and runtime **1.0.83**. Every
opened layer was visible and active through the actual registry. Normal close
went through the captured ownership handle, then clean history resumed through
a fresh layer. Idle revocation denied a held platform permission waiter and
removed ownership independently. Hard stop while holding the producer lock
joined cleanup, and uncertain histories remained ineligible for resume. All
three runtimes exited normally; contexts, claims and credential observers were
released. No native tools executed.

**135 focused tests** passed across the registry, concurrency/scheduler,
lifecycle consumers and Copilot layer. These include retained cleanup failures,
stale snapshots, startup collisions, cancelled close waiters, late owners,
reservation timing, replacement contexts, scheduler wait ceilings, retention
protection and concurrent shutdown. **1,043 offline Copilot tests plus 153
subtests** passed. Ruff and diff checks passed. Full PostgreSQL-backed proxy,
audio, dashboard and satellite regression results are recorded on the PR.

The live run uses one actual token with controlled account, network-target
discovery and knowledge fixtures. It does not establish PostgreSQL account
provisioning, OAuth refresh, actual chat deletion during model execution, or
graceful partial-history preservation. Those consumer behaviors have focused
integration tests, distinct from the runtime probe.

## Remaining registration gates

Copilot remains unregistered. Explicit config/account selection, runtime/storage
provisioning, generic dashboard inputs and authorized cold-resume discovery are
still required. This registry is not an idle timeout service; Copilot currently
closes through its caller or credential/context/runtime invalidation. Automatic
idle cleanup, prewarming and generic subscription rebinding need separate
qualification before broader exposure. MCP, native terminals, satellites,
automation and the full [parity matrix](copilot-parity.md) remain open. See the
[registry contract](copilot-session-registry-contract.md) for the precise
ownership and existing-engine boundaries.
