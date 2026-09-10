# Copilot permission and user-input contract

Audited against the installed **github-copilot-sdk 1.0.13** wheel, targeting the
already pinned **native runtime 1.0.83 / protocol 3**. The field inventory is a
source-contract audit; the implementation section separately identifies observed
live behavior. The permission bridge handles requests
that reach the host; it is **not yet a complete authorization gate for every
native tool execution**. Runtime registration remains blocked on that distinction.

## Provenance

Primary inspected files are the installed wheel's `copilot/session.py`,
`client.py`, `tools.py`, `generated/session_events.py`, and `generated/rpc.py`.
Public exports expose generated types through `copilot.session_events` and
`copilot.rpc`. Release-tag source and documentation were also checked:
[Python SDK](https://github.com/github/copilot-sdk/blob/v1.0.13/python/README.md),
[session dispatch](https://github.com/github/copilot-sdk/blob/v1.0.13/python/copilot/session.py),
[pre-tool-use hook](https://github.com/github/copilot-sdk/blob/v1.0.13/docs/hooks/pre-tool-use.md).
Current `main` is not the contract.

Installed file SHA-256 values:

| File | SHA-256 |
|---|---|
| `session.py` | `5875be04c7fc65e7b9f11b4f1b7ce60d720167dfa6c1f4942f158be8cb33813f` |
| `client.py` | `7cb524f79cf8431399b8292eeccb999ddf26e3b9c32c252680a29c991c55e8b9` |
| `tools.py` | `0573005dad34b59c927237cec4ca3188250284488bee6cb41da49a7ae0a53db2` |
| `generated/session_events.py` | `2fa7175a66dbd5d76c9db9d33ff6d3b18dd383fd8065838cbc94893d2eb885aa` |
| `generated/rpc.py` | `0dd7ca6eb6c816fb4d1e672eeb8a8c33b755a1b85708cc2d24d820d499cbcb3c` |

## Permission input

`on_permission_request(request, invocation)` accepts a typed generated request
variant. `invocation` contains `session_id` and `managed_settings_enabled`; it
does **not** contain the native permission request ID or working directory.
Use the independently pinned session identity and sandbox working directory,
not a model-supplied replacement. The callback may return synchronously or await.

The following names are **Python attributes**. Serialized events use camelCase,
for example `full_command_text` becomes `fullCommandText` via `to_dict()`.
All variants have optional `tool_call_id` and `managed_approval_required`.

| `kind` | Required fields | Additional optional fields |
|---|---|---|
| `shell` | `can_offer_session_approval: bool`, `commands: list`, `full_command_text: str`, `has_write_file_redirection: bool`, `intention: str`, `possible_paths: list[str]`, `possible_urls: list` | `command_segments`, `warning`, sandbox-bypass fields below |
| `read` | `intention: str`, `path: str` | sandbox-bypass fields |
| `write` | `can_offer_session_approval: bool`, `diff: str`, `file_name: str`, `intention: str` | `new_file_contents: str`, sandbox-bypass fields |
| `url` | `intention: str`, `url: str` | `redirected_from: str`, sandbox-bypass fields |
| `mcp` | `read_only: bool`, `server_name: str`, `tool_name: str`, `tool_title: str` | `args: Any`, `permission_recommendation` |
| `custom-tool` | `tool_description: str`, `tool_name: str` | `args: Any`, `skip_permission: bool` |
| `hook` | `tool_name: str` | `hook_message: str`, `tool_args: Any` |
| `memory` | `fact: str` | `action`, `assisted_approval`, `citations`, `direction`, `reason`, `repo_nwo`, `scope`, `subject` |
| `extension-management` | `operation: str` | `extension_name: str` |
| `extension-permission-access` | `capabilities: list[str]`, `extension_name: str` | common fields only |
| `extension-env-access` | `environment_variables: list[str]`, `extension_name: str` | common fields only |
| `factory` | `approval_key: str`, `can_persist_approval: bool`, `description: str`, `name: str`, `operation`, `phases` | declared/effective credit, concurrency, total-subagent and timeout limits |

Sandbox-bypass fields on shell/read/write/url are
`request_sandbox_bypass: bool | None` and
`request_sandbox_bypass_reason: str | None`. Reject requested bypass in this
bridge: a tool approval never authorizes weakening OtoDock's outer sandbox.

Shell `commands` elements contain `identifier: str` and `read_only: bool`;
`command_segments` contain `identifier` and `full_command_text`; `possible_urls`
elements contain `url`. These are runtime descriptions, not proof that a shell
command has no other effects. Preserve the complete command for authority
evaluation; do not approve only a parsed executable prefix. Likewise, MCP
`read_only` and `permission_recommendation` do not replace OtoDock authorization.

Recommended canonical projection for existing authority checks:

| Native request | Existing authority input |
|---|---|
| shell | `Bash`, complete `command`, independently pinned `cwd` |
| read | `Read`, exact `file_path` |
| write | `Write`, exact `file_path`; retain offered contents/diff for review |
| url | `WebFetch`, exact `url`; preserve redirect context for validation |
| MCP | `mcp__<server>__<tool>`, complete dictionary arguments |
| custom tool | Explicitly configured canonical tool-name mapping and complete dictionary arguments |

This projection does not establish canonical schemas for every native tool.
Unknown kinds, unknown custom tools, malformed required fields, ambiguous MCP
names, and non-dictionary arguments must not become generic approvals. An
authority answer that rewrites input cannot be applied through a permission
decision: there is no replacement-input field. Reject the mismatch instead of
approving the original request under authorization for modified arguments.

## Permission decisions and transport

Use the generated constructors; their `kind` discriminators are fixed class
variables and must not be supplied as constructor arguments:

| Python constructor | Wire decision | Bridge interpretation |
|---|---|---|
| `PermissionDecisionApproveOnce(approved_interactively=None)` | `approve-once` | Only after explicit authority approval; mark interactive only with actual human provenance |
| `PermissionDecisionReject(feedback=None)` | `reject` | Explicit denial; keep feedback sanitized |
| `PermissionDecisionUserNotAvailable()` | `user-not-available` | Unavailable authority/user, closed admission, or unsupported request |

Other supported variants exist, but are **not** safe substitutes for one-shot
authorization: `approve-for-session` takes optional `approval` and/or `domain`;
`approve-for-location` requires `approval` and `location_key`;
`approve-permanently` requires `domain`. Their scopes can change later prompting.
The generated union also accepts outcome-style variants such as `approved`,
`approved-for-session`, `cancelled`, and several `denied-*` forms. Do not use
past-tense outcomes as the bridge's normal command to grant permission.

`PermissionNoResult()` is an SDK sentinel, not a denial. Event dispatch sends no
response for it, leaving another client to resolve the request. The legacy
direct callback path converts that sentinel to user-not-available. The SDK's
`approve_all` helper throws when managed settings are enabled and abstains for
`managed_approval_required=True`; it is not a safe default for this bridge.
Until an explicit enterprise approval authority and human provenance are wired,
reject managed-approval requests rather than treating a general allow as proof.

`AttributedPermissionResult` adds `PermissionDecisionContext` with `outcome`,
`source`, `surface`, and optional `response_capability`. This is informational
telemetry and does not change permission behavior.

The `permission.requested` event data has required `request_id` and
`permission_request`, plus optional `agent_mode`, `prompt_request`,
`resolved_by_hook`, and `risk_assessment`. `permission.completed` carries
`request_id`, `result`, and optional `tool_call_id`. These native IDs belong to
the event envelope, not the callback's request object.

Explicit reply API:

```python
await session.rpc.permissions.handle_pending_permission_request(
    PermissionDecisionRequest(request_id=native_id, result=decision),
    timeout=bounded_timeout,
)
```

The RPC response has `success: bool`, false when the request was already
resolved. This generated RPC `PermissionRequestResult` is distinct from the
same-named SDK callback type alias. `pending_requests()` returns `.items`, each
containing `request_id` and a user-facing `request: PermissionPromptRequest`;
it reconstructs pending prompts from event history. Do not assume it returns
the original full typed request or that local callback completion means reply
delivery succeeded.

## Native approvals can bypass the callback

Verified SDK surfaces expose all of these paths:

1. `Tool.skip_permission=True` serializes `skipPermission` and explicitly permits
   a custom tool to run without a permission prompt.
2. Permission events with `resolved_by_hook` skip the SDK permission callback.
3. Permission mode `allow-all`, `set_approve_all`, configured approved rules,
   automatic read approval, unrestricted paths/URLs, session approvals, location
   approvals, and permanent domains can affect whether a new prompt exists.
4. `PreToolUseHookOutput` can return `permissionDecision` of `allow`, `deny`, or
   `ask`, with `modifiedArgs`, reason, additional context, and output controls.

Consequently, configuring a permission callback is not evidence of every-tool
policy coverage. Do not register the execution engine on that basis.

The pinned RPC schema supports `permissions.set_mode(manual)`,
`reset_session_approvals(include_location=...)`, and `configure(...)` with
`approve_all_read_permission_requests`, `approve_all_tool_permission_requests`,
`rules`, `paths`, and `urls`. `PermissionRulesSet` contains `approved` and
`denied` lists. Paths include `workspace_path`, `additional_directories`,
`include_temp_directory`, `unrestricted`; URLs include `initial_allowed` and
`unrestricted`. Resetting session approvals alone is not evidence that every
other bypass was reset. Nor should enterprise denial rules be cleared casually.
Actual bootstrap/resume configuration and resulting native tool coverage still
need a bounded live test before a universal-gate claim.

The pre-tool-use hook input contains `sessionId`, `timestamp`,
`workingDirectory`, `toolName`, and full `toolArgs`; it contains no tool-call ID.
It is a candidate for enforcing policy before tool execution even when native
permission prompting is cached. Complete integration needs the exact enabled
native tool catalog, argument schemas, hook coverage under cached/hook-resolved
approvals, and a strategy that does not authorize rewritten arguments by mistake.
That work is outside the owned-request bridge slice.

## User input and cancellation

The legacy `on_user_input_request(request, context)` request is a dictionary
with `question`, `choices`, and `allowFreeform`. The SDK supplies missing choices
as `[]` and missing `allowFreeform` as `True`; context only contains
`session_id`. No request ID, tool ID, or cancellation capability reaches this
callback. Return exactly `{"answer": str, "wasFreeform": bool}`. The SDK casts
and forwards these values; the bridge must validate them against the presented
choices and freeform setting. A selected answer must match an offered choice;
a freeform answer requires permission for freeform input.

**Legacy user input has no explicit cancel/decline response.** Do not invent
one or send an empty answer as proof of user assent. Cancellation/unavailability
must fail through a controlled, sanitized host exception and remain subject to
supervisor shutdown/settlement checks. A separate raw pending-input RPC exists:
`session.rpc.ui.handle_pending_user_input` accepts
`UIHandlePendingUserInputRequest(request_id, UIUserInputResponse(answer,
was_freeform))`; this also has no cancel flag.

The native `user_input.requested` event includes `request_id`, `question`, and
optional `allow_freeform`, `choices`, `tool_call_id`.
`user_input.completed` includes `request_id` and optional `answer`,
`was_freeform`. Subscribe before create/resume and track those IDs separately
from host callback ownership; identical question text is not a request identity.

The supervisor now implements this separate native inventory. A requested ID
remains pending after its host handler returns, and only native completion
retires it. Completion tombstones prevent exact-frame and semantic request
replays from resurrecting retired questions; completion-before-request is also
conservative. Changed-payload event-ID replays or malformed request IDs fail the
session. These IDs join the pending-message inventory without converting an
unknown native queue or external permission inventory to empty. Native question
completion invalidates any preceding idle checkpoint, so that earlier idle
cannot be reused as final completion evidence.

Structured elicitation is a different contract. Explicitly selecting
`ask_user_variant="elicitation"` also needs `on_elicitation_request`. Its
single context argument includes `session_id`, `message`, and optional
`requestedSchema`, `mode` (`form` or `url`), `elicitationSource`, and `url`.
Its result supports `action: accept | decline | cancel`, with optional `content`
mapping to string/number/bool/list-of-string values. Do not silently translate
this broader schema/browser flow into legacy questions.

## Pending ownership and acceptance criteria

In 1.0.13, permission event dispatch calls `asyncio.ensure_future` without the
external-tool task registry's deduplication or completion cancellation. A
duplicate permission event can launch another handler, and a completion event
does not cancel a held permission handler in this SDK path. The SDK executes
registered event listeners before its internal permission dispatch, so an
explicit event-ID/request-ID inventory can be populated first.

The bridge must own every permission/question wait with bounded admission,
deadline, cancellation and join. Invalidate the coordinator's settlement revision
before each inventory mutation. Reject new waits after close and during accepted
control settlement. A host-generated callback ID is valid for ownership; it is
not interchangeable with the native event request ID for replies or deduplication.

SDK permission handlers log exceptions with tracebacks. Return typed denial for
expected policy failures and keep exceptional errors sanitized before they reach
SDK logging. `asyncio.CancelledError` is not caught by those `except Exception`
blocks: cancellation can stop the callback before the SDK sends any reply.
Conversely, a normally returned decision is followed by a separate awaited RPC.
Therefore host inventory empty, native inventory empty, and processing false
must all be reconciled under the existing event/revision fence. `session.idle`,
abort ACK, or a joined host wait is not sufficient individually.

Required evidence for this slice: exact typed request mapping; allow/deny and
unavailable decisions; cancellation with no fabricated question answer; pending
ownership through abort/close; refusal of
unknown/bypass/managed requests; sanitized failures; and at least one real
runtime request flowing through the bridge. Remaining native tool gate and
structured elicitation gaps must stay explicit after those checks pass.

## Implemented native cancellation reconciliation

The bridge uses the existing shared OtoDock authority for mapped permission
requests, with owned host request admission/cancellation and strict one-shot
approval. Expected refusal returns a fixed denial; unavailable legacy questions
raise a sanitized error instead of fabricating an answer. No global native
approval cache or every-tool pre-tool-use gate is added by this slice.

A held-approval live abort exposed another lifecycle gap: native
`tool.execution_start` can lack its matching `tool.execution_complete` even
after permission completion, host request cancellation/join, and aborted idle.
The initial observed permission completion outcomes were one `approved`, one
`cancelled`, and one `denied-interactively-by-user`; all carried `toolCallId`.
This observation supports a narrowly correlated cancellation path, not treating
every permission denial as proof that its tool has stopped.

The supervisor therefore tracks **native permission cancellation evidence
separately from host tool-callback cancellation proof**. Evidence requires an
observed `permission.requested` with its native request/tool relationship and a
matching `permission.completed` request ID and tool ID whose result kind is
exactly `cancelled`. It applies only to that still-open tool. Missing or
contradictory correlation, approval outcomes, other denial kinds, and unknown
outcomes cannot close a tool badge through this path.

The coordinator accepts this evidence only within an established control
settlement: an accepted abort with its matching observed aborted idle and a
current full settlement snapshot, or an accepted interrupt with two matching
full snapshots separated by the processing barrier and protected by the same
event/host-mutation fence. Snapshots must establish no native processing,
nonterminal background tasks, pending permissions, host callbacks/requests, or
queued messages/questions. Stale evidence cannot be credited to a later turn.

Only then may translation emit an explicit error `TOOL_RESULT` and the normal
single `DONE`. This reports confirmed cancellation of the unfinished operation;
it does not claim success, absence of earlier side effects, rollback, or graceful
history preservation. `denied-interactively-by-user` and the other denial
outcomes remain unusable as tool-stop proof in this implementation. The bounded
[final live regression](evidence/copilot-permission-live.json) passed in 32.278
seconds: one fixture execution, one native cancellation, three denial outcomes,
one aborted-turn DONE and normal runtime cleanup. Two of those denials were
repeated calls refused after the fixture's one-shot allowance. This is controlled
host-fixture evidence; native questions, interrupt at a permission prompt and
every-tool gate coverage are not live-qualified by that recording.
