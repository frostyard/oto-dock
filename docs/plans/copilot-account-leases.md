# Copilot account and credential lease foundation

This slice adds account storage, explicit payer scope, typed runtime credentials,
private session state and a process-local authorization observer. It remains
unregistered: there are no Copilot connect routes, engine selectors, GitHub App
installations or deployment changes. C1 qualification and C2 account integration
are still in progress; this is not full P01–P16 acceptance.

## Explicit account selection

[`copilot_account_store`](../../proxy/storage/copilot_account_store.py) reuses the
existing PostgreSQL subscription table and Fernet encryption. Copilot tokens have
distinct authentication types; they are not generic API keys or Claude/OpenAI
OAuth blobs. Shared contribution defaults off, including for administrators.

The caller supplies one account ID and an explicit
[`CopilotAccountScope`](../../proxy/core/layers/copilot/credentials.py):

- Personal scope requires a nonempty OtoDock user ID, matching account ownership,
  personal use enabled, an active account and a still-existing owner.
- Platform scope requires an explicitly contributed active account owned by a
  current administrator. It does not authorize the caller to run an agent; the
  outer service must first enforce agent and organization access.

No lookup chooses another account, reads `gh` credentials, borrows an administrator's
personal subscription or interprets an empty user ID as platform scope. The
store's creation API is trusted internal code: token prefixes select a channel
but do not establish GitHub identity or entitlement. Future connect routes must
validate the token and obtain an authoritative stable provider identity before
calling it. GitHub's SDK login display name is insufficient for that binding.

Credential replacements lock the selected row and compare the expected revision.
Only its owner can replace it; principal and authentication kind remain fixed.
A new UUID revision prevents a stale concurrent refresh from overwriting the
winner. Replacing material does not reactivate a disabled account. Public account
records omit ciphertext; typed credential representations omit the token.

## Session authorization and expiry

[`CopilotLeaseGuard`](../../proxy/core/layers/copilot/lease.py) pins the complete
credential snapshot and acquisition scope. Its store bridge reads encrypted
account state off the event loop. The supervisor revalidates before each new
prompt or steering submission while holding the writer lock, then rechecks
session admission after the database await. A changed account or credential
closes the session instead of retrying with a different payer.

A separate observer revalidates while the consumer is paused or the session is
idle. Known credential expiry has its own timer, so a stalled database read
cannot extend its local lifetime. Failure to read authoritative state denies
use. A stopped guard cannot silently become valid again if settings are restored.
A fresh credential requires a new guard and runtime; safe sequential history
resume is separate from credential refresh.

This is **process-local observation**, not a durable database lease or an atomic
transaction spanning GitHub execution. Under normal event-loop scheduling,
store revocation is detected within the configured polling interval plus a
bounded read (defaults: 2 seconds and 5 seconds). Work may start between the last
successful read and an external revocation. Remote token revocation is not
inferred by a local database read; provider authentication failures still need
classification by the full execution layer. Unknown user-token expiry cannot
prove a positive minimum lifetime. No provider refresh token is minted here.

## Runtime and state boundary

The [runtime owner](../../proxy/core/layers/copilot/runtime.py) accepts a typed
credential. User tokens use the SDK's explicit token option; installation tokens
use the dedicated child runtime environment channel. Caller-supplied credential
and direct API overrides are rejected before the selected channel is injected.
The [authentication contract](copilot-auth-contract.md) records official sources,
expiry/restart constraints and unverified organization prerequisites.

[`PrivateCopilotSessionState`](../../proxy/core/layers/copilot/session_state.py)
allocates a distinct private directory beneath a trusted host root, outside a
shared agent workspace. Callers mount it at `/var/lib/otodock/copilot` for one
logical session. It is retained across an intentional runtime replacement and
removed only by explicit disposal. The helper checks root/allocation ownership
and filesystem identity before accessing or deleting it; it does not copy state
to a different account or write credential files.

The first sandbox restart experiment found that supplying this directory as an
MCP mount silently omitted it: community MCP mounts only admit sources beneath
specific agent/MCP roots. The runtime now requires the state helper for typed
credentials and appends its validated private bind after the builder's mounts.
The destination is fixed and cannot overlap runtime assets. This preserves the
existing MCP mount restrictions while making session-state persistence explicit.

The full factory must keep one logical session bound to the same payer during
history resume, keep this directory out of workspace sync, and join runtime
cleanup before discarding it. This helper is not an exclusive cross-process
writer lock. A directory alone is not evidence that a session can resume.

## Validation and remaining work

Offline tests exercise typed token channels, missing/expired lifetimes, local
scope validation, store-read failure, replaced credentials, paused consumers,
independent expiry, startup/shutdown races and private state ownership. The
PostgreSQL suite adds account encryption, three OtoDock users/two payer identities,
sharing/demotion, disabling/deleting accounts, corrupt records and concurrent
compare-and-swap replacement. These identities are test fixtures, not evidence of
two real subscriptions or organization entitlement.

The bounded [live authentication probe](copilot-auth-contract.md) uses the same
explicitly selected user token in sequential sandbox runtimes. Credential
generation replacement in a controlled source proves the restart and observer
mechanics; it is not a genuine OAuth refresh or installation-token transaction.

Remaining gates include authenticated connect/reconnect/disconnect routes and UI,
stable provider identity verification, OAuth/App token issuance and real expiry
refresh, model entitlement checks, usage/payer attribution, a complete account-
bound execution factory, persisted writer leases, native tools and permissions,
remote transfer/adoption and all supported platforms. Existing engines retain
their current account pool behavior; this slice adds no database migration.
