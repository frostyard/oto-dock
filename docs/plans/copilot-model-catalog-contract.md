# Copilot account model catalog contract

The local Copilot chat page and settings preview load models for an explicitly
selected personal account and agent. **Load available models** performs a
bounded metadata lookup; the user then selects a model before the first Send.
Disabled or unknown-policy models cannot be selected. Saved conversations keep
their original model and may be read/resumed without loading a new catalog.

## Request and authorization

`POST /v1/copilot/chat/models` accepts only `{agent, account_id}` and returns
`{models: [{id, name, available, policy, multiplier}]}`. It requires the same
human cookie, exact Origin and JSON checks as chat creation. GET does not start
discovery. Responses carry `Cache-Control: no-store`.

Each request reserves the preview's global/per-user capacity before awaiting
work and acquires normal local execution capacity before runtime startup.
Discovery and chat therefore share admission limits. The service rechecks
current human, agent, workspace configuration and exact personal credential
generation before returning the result. Account selection cannot borrow a
platform payer or fall back to another personal account.

The service deadline is 45 seconds. Client disconnect, cancellation, timeout and
application shutdown join captured-owner cleanup. Results are returned only
after the runtime has stopped and temporary state has been discarded. Failed
cleanup retains ownership/capacity and returns an error rather than publishing
a successful inventory.

## Runtime boundary

Discovery uses the pinned SDK 1.0.13/runtime 1.0.83 and the normal Linux sandbox
launcher. Its workspace, MCP and home directories are empty private temporary
trees; actual agent workspaces, credential homes and saved histories are not
mounted. It owns a watched credential lease and platform security context.
There is no native session creation/resume, inference, tool call, conversation
row, durable profile or saved history. Generic turn/control entry points reject
a catalog owner.

The SDK's public `list_models()` coerces raw IDs/names to strings and billing
multipliers to floats. This pinned adapter instead uses the same underlying
`models.list` RPC and validates its raw response before projecting public rows.
An SDK upgrade must requalify that internal call alongside the rest of the
versioned adapter.

The catalog accepts at most 200 unique models. IDs and names are nonempty,
printable strings of at most 256 characters. Known policy states are `enabled`,
`unconfigured` and `disabled`; missing policy maps to `unconfigured`, while an
unrecognized state maps to `unknown`. `available` means the current policy
permits selection, matching the existing runtime startup checks. It does not
establish future quota or inference entitlement. Optional reported multipliers
must be finite numbers from 0 through 1,000; booleans and numeric strings are
rejected. Provider prose, authentication data and private error details are not
returned.

## Dashboard behavior

Loading is explicit. There is no mount/focus polling, automatic retry or browser
persistence. Catalog state is bound to the human, agent, selected account and
its revision/status/personal-use/expiry metadata. Changing that selection clears
models and the chosen model, and late responses cannot populate the new scope.
Only one catalog request may be pending in a panel. New-owner actions wait for
that request to settle; leaving the page aborts it and server cleanup retains
ownership until finished.

Loading, error, empty and policy-disabled results have visible states and an
explicit retry/reload action. Chat startup still revalidates the chosen model
against the selected account; loading a catalog does not freeze vendor policy.

## Remaining scope

See the [parity matrix](copilot-parity.md). General engine registration, shared
payers, persisted model defaults, reasoning controls, attachments, queues,
MCP/delegation, remote/terminal workflows and organization automation remain
separate qualification gates. This slice changes no running deployment.
