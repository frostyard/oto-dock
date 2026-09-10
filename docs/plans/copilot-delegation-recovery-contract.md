# Copilot delegation recovery contract

A Copilot coordinator's delegated work now has a durable outcome ledger separate
from its native conversation events. Opening an archived conversation or choosing
**Refresh worker results** reads that ledger. It never starts a model, retries a
worker, or resumes an interrupted parent.

This extends the [owned delegation contract](copilot-delegation-contract.md).
Targets remain local Claude Code or Codex agents, with four concurrent host
invocations and a 120-second worker lifetime. Copilot itself is not yet available
as a repository worker.

## Identity and finalization

The worker allocates its task, run, session and chat IDs before execution. A
row-locked transaction stores those IDs with the original human, source agent,
conversation, writer generation, native tool invocation and argument digest.
The same transaction saves the hidden request audit and advances the parent
revision. The scheduler can only use that allocation after reservation commits.
Duplicate invocations, including legacy request audits, never dispatch again.

Cleanup joins startup, the runner, the native process and readers, the producer,
the pump and subscription ownership. It then checks the exact terminal run
binding and commits a bounded outcome through the original receipt. Receipt
finalization does not depend on parent connection, account/access authorization,
writer generation, or a surviving original user/source conversation. Receipt
identity survives cascading deletion independently; it does not make deleted
history publicly accessible. If deletion also removes or changes the run binding,
the worker cannot verify its outcome and remains unverified. A failed outcome
write retains worker ownership.

A **settled** snapshot means joined cleanup and the saved terminal outcome.
It may say completed, failed, cancelled or limit exceeded. A worker that never
created an execution is saved as failed with `execution_created: false`.
An **unverified** snapshot has no public status, output or execution-created
claim. A terminal generic run row alone cannot promote it to settled.

The result is immutable once committed. Identical finalization is idempotent;
conflicting identities or results fail. Later generic task activity cannot
rewrite a saved snapshot. This protects dispatch identity; it cannot promise
exactly-once effects in repositories or external services.

## Restart and access

Startup loads unresolved receipts before generic orphan processing, even when
Copilot preview configuration is disabled. Their session/chat identities remain
restricted from generic mutations and nested delegation. Startup never adopts
an execution or infers cleanup from a missing/terminal run. A malformed,
conflicting, unavailable or oversized recovery read fails startup rather than
silently omitting restrictions.

Historical results require the original human and current access to the source
agent, as native history does. Reading does not select a current payer or check
new target credentials. Private receipt, session, generation and digest fields
are absent from the public snapshot. Deleted parent history remains unavailable.

The dashboard correlates exact worker identities independently of native events.
A settled ledger snapshot can recover a result whose final stream event was
never saved. Unverified cleanup suppresses apparent success; stale unverified
reads cannot downgrade settlement already observed. Malformed or conflicting
worker evidence marks the worker summary unavailable while native text remains
readable. Worker inspection still uses the existing authorized task route.

## Bounds and remaining work

Each conversation permits 32 lifetime reservations, each with at most 16,384
UTF-8 bytes of output. The public ledger has a separate 4 MiB encoded JSON bound;
it does not consume the native history's 1 MiB event budget. Startup accepts at
most 10,000 unresolved receipts and fails on overflow instead of restoring a
partial list.

An unresolved receipt after a crash remains quarantined. Automatic reconciliation,
operator release, cleanup re-adoption and fresh-conversation continuation are
not implemented. Legacy requests without a receipt cannot be reconstructed by
matching agent names or prompts. Incomplete native parent sessions remain
incomplete. An uncertain reservation commit can leave an unverified receipt even
though no worker started; it is never automatically retried. A live restart test
with real mixed-engine workers and the complete
CTO/repository/QA acceptance workflow remain qualification work.

See [validation results](copilot-delegation-recovery-results.md).
