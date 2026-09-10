# Copilot local ExecutionLayer results

`CopilotExecutionLayer` implements the existing local lifecycle interface using
the account-bound session factory. A trusted caller supplies a typed
`CopilotAgentConfig` with explicit account/scope, driver, model and reviewed tool
subset. Generic credential environments, MCP configuration, provider/terminal
options and unsupported clients are rejected before permission registration.
The class remains absent from the global engine registry.

## Ownership and controls

Startup reserves the platform session ID across adapter instances, rechecks
other engines immediately before registration, and captures the actual stamped
SecurityContext. Duplicate or cancelled startup cannot erase another owner's
permission state. Independent factory closure triggers a layer reaper that joins
cleanup and denies owned platform waiters, including with an idle consumer.
Replacing the context revokes the old owner without letting it delete the new
registration.

Producer locks are separate from supervisor dispatch locks. They carry the
session generation so an old queue-drain task cannot send into a replacement
session with the same ID. Hard abort joins cleanup and returns False, matching
the ExecutionLayer contract for a killed stream. Steer and queued interruption
return False without dispatching anything; graceful partial-history preservation
remains unqualified. Failed cleanup retains a blocking ownership claim and never
reports confirmed process death.

Explicit `config.resume=True` verifies the full durable profile in the factory.
The generic `can_resume_session` call lacks account/profile authority, so it
conservatively returns False and generic resume is not advertised. Identical
model/mode requests are no-ops only when they match the actual session; changes
and unsupported control commands are rejected. Permission replies must match
the exact session owning the request.

## Sandbox integration

The adapter uses the actual shared sandbox resolver. A new internal
`trusted_runtime_mounts` channel admits host-selected runtime asset directories
read-only without widening the MCP manifest source allowlist. Invalid/protected
destinations and writable runtime mounts are rejected; later MCP declarations
cannot shadow admitted assets.

An opt-in isolated configuration-home overlay mounts one private scratch home at
the session cwd's `.claude` path. The scratch helper uses stable hashed IDs,
pinned directory identities and exact owner/mode checks. It never copies another
engine's credentials, repairs existing permissions, or deletes retained data.
The layer rejects mounts exposing the private parent or sibling homes. This
replaces the corresponding configuration-home mount only; it is not a claim
that every other credential-bearing workspace path is absent. Native tool/path
policy and the remaining credential-isolation qualification still apply.

Both new sandbox options default off/empty for existing engines. Adding their
fields changes the canonical sandbox digest; histories from older experimental
factory builds fail profile matching instead of silently adopting the new
configuration.

## Evidence and remaining gates

The [three-turn live probe](evidence/copilot-execution-layer.json) passed in
**63.851 seconds**, using SDK **1.0.13** and runtime **1.0.83**. The second
runtime recalled the first turn's marker through a fresh layer and record
manager with a changed credential revision. Duplicate startup preserved the
existing context and did not start another runtime. Idle revocation independently
joined cleanup, removed the owned context and denied the held platform waiter.
Hard abort returned False while the consumer was paused after its first text,
with the producer lock held and no observed DONE. Both revoked and aborted
histories were rejected for resume before spawning. All three runtimes closed
normally; all contexts, layer claims and credential observers were released.

The offline Copilot suite passed **1,043 tests plus 153 subtests**. The combined
real-platform layer, authority and new sandbox-mount suites passed **189 tests**,
including 60 new execution-layer and 64 new mount cases. The new lifecycle
observer and private scratch-home tests cover cancelled/late waiters, cleanup
failure, exact directory identities and concurrent creation. Ruff and diff
checks passed. Full repository CI is recorded on the implementation PR.

The live probe uses one actual GitHub token with a controlled scoped credential
source. It does not prove PostgreSQL provisioning, OAuth refresh, two real payer
accounts, model-tool execution, user-facing dashboard flows or native graceful
abort. Its held permission is an actual platform waiter created by the probe,
not a native tool approval request.

Global registration still requires explicit Copilot config/account selection,
runtime/storage provisioning, common session-registry/concurrency integration,
engine allowlists, shutdown/deletion/retention hooks and generic resume/input
support. The existing dashboard queue sends `inject_time=True`, which this
bounded adapter rejects; attachments and other engine-specific send options
also remain open. These concrete entry-point blockers are listed in the
[contract](copilot-execution-layer-contract.md). MCP, terminals, remote workers,
automation and the remaining [parity matrix](copilot-parity.md) are not completed
by implementing the lifecycle class.
