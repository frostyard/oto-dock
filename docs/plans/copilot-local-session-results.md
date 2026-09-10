# Copilot account-bound local session results

`CopilotLocalSession.open` now composes the scoped account lease, real platform
permission bridge, private durable history, sandbox runtime, native tool policy,
attached-shell adapter and CommonEvent supervisor into one owned lifecycle.
Copilot remains unregistered; this is a local composition boundary for the next
execution-layer integration, not a selectable product engine.

## Resulting behavior

The caller supplies an explicit account, payer scope, driving user, platform
session ID, model and reviewed native tool subset. It authenticates agent access
and registers the platform security context before opening the factory. The
factory checks that context against the sandbox configuration and pins its
identity and value for the runtime lifetime. It checks native authentication,
model availability and policy, then reauthorizes the exact lease immediately
before creating or resuming the SDK session. No raw SDK object is exposed.

A host-only record binds the native session ID and private state allocation to
the account/principal, driver, platform session, workspace, model, native tool
profile, SDK/runtime versions and configuration digest. Exclusive nonblocking
file locks prevent concurrent writers. Private inode identities and strict
metadata checks reject replaced, malformed or mismatched records. A fresh
credential revision can resume clean history for the same account and profile;
credentials are never persisted in these host records.

Records are marked active before runtime startup. Only an observed completed
turn followed by normal owned cleanup can mark history ready to resume. A crash,
partial startup, provider/transport failure, abandoned stream, control operation,
revocation or uncertain cleanup leaves history active and ineligible for automatic
resume. A previous DONE cannot hide a later failure. Active history is retained
for future explicit recovery, not silently replayed or deleted.

The independent credential observer and half-second context/runtime observer
close idle or paused owners on authority or transport loss. Every submission
reauthorizes within the supervisor's writer lock. Late SDK acquisitions after
cancelled startup are rejected and cleaned up. Close joins owned cleanup even
when its caller is cancelled; it releases the writer lock and lease observer.
Provider error events are sanitized at the factory boundary.

## Evidence

The [live sandbox probe](evidence/copilot-local-session.json) passed in
**17.658 seconds** using SDK **1.0.13**, runtime **1.0.83**, and two bounded
no-tool model turns. It used the actual factory, sandbox builder, registered
SecurityContext, permission bridge, records, lease guard and runtime owner.
The second runtime recalled the marker after a clean close, reopening private
history through a new record manager with a changed credential revision.
Concurrent writers and changed profiles were rejected before another runtime
started. Idle revocation closed the runtime, and subsequent resume was rejected.
Both runtimes closed normally and all credential observers stopped.

The complete offline Copilot suite passed **999 tests and 153 subtests**.
This includes 75 new durable-record tests and 48 factory/health tests covering
account isolation, cross-process writer exclusion and crash quarantine,
filesystem/metadata faults, startup cancellation, context changes, paused
consumers, provider errors after DONE, dead runtimes and uncertain history.
Separately, **65 real platform authority tests** passed, including 14 new
owner-validity cases with held human approvals/questions and plan-mode checks.
Those focused tests use the actual permission service without database fixtures.
Full repository regression results are recorded on the implementation PR.

## Scope and remaining work

The live probe uses one real GitHub CLI token and an in-memory scoped credential
source. It does not prove PostgreSQL-backed provisioning, real OAuth refresh,
two actual payer accounts, model entitlement for all future requests, a full
proxy process restart, or dashboard approval. It exercises no model tool calls;
native-tool and shell-control evidence remains in the preceding slices.

The caller contract still needs to be wired into authenticated session entry
points, platform routing cleanup, production storage roots and engine selection.
Mode/client/meeting routing is freshly bound on resume; the durable profile pins
the security context and sandbox, not those transient routes. Abort and interrupt
return supervisor acknowledgements, not the existing ExecutionLayer's graceful
abort boolean. Controlled history therefore remains ineligible for resume until
that integration contract is qualified.

MCP, custom tools, attachments, terminal/headless handoff, remote workers,
in-flight recovery and the remaining parity matrix remain open. Failed-record
inspection, explicit recovery/discard and storage retention also need product
work. See the [composition contract](copilot-local-session-contract.md) and
[full parity plan](copilot-parity.md).
