# Copilot delegation recovery validation

This phase adds durable worker identity and cleanup-verified outcomes without
changing the native Copilot tool catalog or inference interface. Validation uses
real scheduler paths with controlled engine resources, independent service
fixtures, PostgreSQL transaction tests and dashboard tests. No new live model
or deployed-service run was performed for this phase.

## Coverage

- Worker tests allocate IDs before dispatch, preserve the scheduler binding,
  join delayed database creation and native cleanup, release subscriptions before
  saving outcomes, and retain claims on identity or persistence failures.
- Service tests require a committed receipt before worker execution, recover a
  saved result when the parent's final event cannot commit, persist cancelled
  outcomes after parent access revocation, and enforce history access before
  reading the ledger. A cold read leaves revision and runtime state unchanged.
- Recovery tests restore restrictions without a native owner, reject partial or
  conflicting recovery data, and require the exact receipt to release a claim.
- Ledger tests cover duplicate invocation and allocation races, transactional
  rollback, owner/generation checks, immutable finish after parent changes,
  deletion retention, schema idempotence, payload bounds and startup overflow.
- Dashboard tests cover ledger-only results, unverified cleanup, identity/result
  conflicts, stale reads, archived refresh without inference and selection
  races. Invalid worker evidence leaves native history readable.

Local PostgreSQL was unavailable; database integration checks run in full CI.
Exact test counts and the final commit's CI status are recorded in the pull
request and [tracking issue](https://github.com/frostyard/oto-dock/issues/1).

These checks establish deterministic recovery behavior. They do not demonstrate
live process re-adoption, recovery of old unrecorded workers, Copilot repository
workers or complete organization-level parity. See the
[contract](copilot-delegation-recovery-contract.md) for operational limits.
