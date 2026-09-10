# Copilot owned permission and question bridge

This slice connects SDK callbacks to the existing OtoDock permission and
question authorities. It remains an unregistered engine component. It does not
enable Copilot chat or establish complete native-tool policy coverage.

## Behavior

`CopilotPermissionBridge` maps supported shell, read, write, URL, MCP and
explicitly bound custom-tool requests to `decide_tool_permission`. It preserves
complete operation arguments, uses a trusted working directory, and permits only
an explicit allow without an argument rewrite. Runtime read-only hints do not
confer authority. Unknown operations, unsupported sandbox bypass, managed
approval requests and malformed inputs are rejected.

The platform adapter resolves live session context and mode for each operation.
It rejects answers after the context identity, contents, mode, client type or
meeting route changes during a wait. Legacy questions use `ask_user_question`;
valid choices and permitted free text are preserved. Unattended sessions do not
wait for a human. The SDK's legacy question response has no cancellation value,
so unavailable answers produce a fixed, sanitized error rather than invented
user input.

The supervisor owns permission/question waits separately from executing host
tools. An abandoned SDK waiter cannot erase a running policy callback. Control
RPCs invalidate outstanding answers before awaiting acknowledgement; close
permanently seals admission. Cancellation-resistant callbacks remain pending,
receive cancellation once, and cannot return a usable late approval. Pending
host requests block DONE. Native question request IDs remain pending until a
matching completion event, because returning a host answer is not reply delivery
proof. New requests after DONE are rejected until a new stream begins.

## Abort while approval is pending

Runtime 1.0.83 emits a tool start before requesting its permission. Aborting a
held permission produces `permission.completed` with outcome `cancelled`, but
can omit `tool.execution_complete`. The original supervisor correctly kept the
unresolved tool open and could not finish the turn.

The fix records exact native request-to-tool correlation and treats native
permission cancellation as separate evidence from host callback cancellation.
Only a matching accepted abort with its aborted idle and a current complete
settlement observation, or a matching accepted interrupt with two fenced
settlement observations around a processing barrier, can close the remaining
tool display with an error and emit DONE. Unknown or contradictory correlation,
ordinary permission denial, missing native cancellation and unresolved work do
not provide this evidence. A late real tool completion cannot duplicate the
display result. This does not prove absence of earlier side effects or preserved
history.

## Validation

Offline tests exercise argument projection, live authority changes, native
request correlation, answer validation, cancellation races, settlement fences
and error sanitization. Integration tests use the actual shared authority and
prompt queues for default, acceptEdits, plan, dontAsk and auto modes, including
external shell denial, private URL and protected path checks.

The opt-in [permission probe](../../scripts/copilot/permission_probe.py) uses the
real Linux sandbox, pinned SDK/runtime, and one inert trusted Python fixture.
Its authority decisions are controlled fixtures. This tests SDK delivery and
supervisor ownership, not a live dashboard approval or arbitrary native tool
execution. Only counts and fixed outcomes are recorded; credentials remain in
memory and temporary state is removed.

The [recorded final run](evidence/copilot-permission-live.json) passed in
32.278 seconds. Its three turns produced one authorized fixture execution,
three native denial outcomes (including two repeated calls rejected after the
one-shot fixture allowance), and one native cancellation. The held approval was
owned and joined on abort; its fixture never ran, and the turn emitted exactly
one DONE despite the missing native tool completion. The runtime closed normally
without forced termination. Native user-question UI interaction and native
interrupt at a permission prompt were covered by offline tests, not this live
recording.

## Remaining registration gates

Permission callbacks alone do not cover every tool: native read approvals,
cached rules, hook-resolved permissions and `skip_permission` can bypass them.
The next policy slice must verify a pre-tool-use floor against the exact enabled
native tool catalog, bootstrap/resume settings, and all supported routes. Native
plan transitions, structured elicitation, brokered MCP, questions across
reconnect/restart, remote and terminal policy remain open. SDK permission-event
dispatch also lacks request-ID deduplication in its callback interface; a
production session factory must own native dispatch/reply reconciliation before
claiming prompts resolve exactly once.

See the [pinned contract](copilot-permission-contract.md) and
[full parity plan](copilot-parity.md).
