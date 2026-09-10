# Copilot owned delegation contract

The personal local Copilot preview can opt into `oto_delegate` when creating a
conversation. This is the first mixed-engine workflow: a Copilot coordinator can
request a repository task, receive its result, then request a QA task. Workers
use an existing local Claude Code or Codex agent configuration. Copilot is still
unregistered in general task/chat routing, and cannot itself be a worker here.

## Setup and authority

Enable and assign `delegation-mcp` to the source agent, configure its delegation
targets, and give the signed-in human access to those targets. Create a new
Copilot conversation with **Allow delegated tasks** checked. The setting defaults
to false, is stored with the conversation, and cannot change during resume.
Legacy rows receive false through an additive database migration.

The host exposes one fixed tool with exactly `agent`, `name`, and `prompt`.
Target names come from the server's current roster and user access. Names are
trimmed printable strings of at most 100 characters; prompts are nonempty and
at most 16,384 UTF-8 bytes. Model-selected credentials, engine overrides, remote
hosts, continuations, arbitrary SDK tools, and arbitrary MCP servers are absent
from this interface. The native six-tool profile remains separately enforced.

Native admission validates the fixed schema, tool identity, session and roster.
Actual dispatch additionally requires OtoDock's current
`mcp__delegation-mcp__delegate` permission decision inside the owned callback.
Neither native admission nor the opt-in setting is permission to execute a task.
The tool is constructed internally with both permission-skipping and built-in
overriding disabled; collisions with any native catalog tool are rejected.

The child adapter applies central spawn authorization with fresh user roles,
source/target policy and the platform kill switch, requests user scope, and uses
the existing scope clamp and editor requirement. The existing task builder picks
the child's engine, model and subscription for that resulting scope. The parent
Copilot account/token is never passed to the worker. Shared-only targets can use
an agent/platform payer under the existing rules; user scope does not imply an
arbitrary personal fallback.

This slice accepts local unattended Claude Code and Codex targets. Targets with
nested delegation or automation MCPs are refused. Configure repository and QA
agents accordingly; the coordinator performs the sequence itself. Each worker
has a maximum 120-second lifetime. Long-running organization plans, nested
chains and automatic parent wakeups require further work.

## Ownership, history and failure

Before dispatch, a row-locked transaction reserves the native tool invocation
under the original human, conversation and writer generation. The reservation
is retained across clean cold resume. A duplicate invocation never starts a
second child, including when the earlier outcome is uncertain. This guards
local dispatch replay; it does not guarantee exactly-once effects in repositories
or external services.

The parent captures each worker owner before its first await. Scheduler hooks
capture allocation IDs, startup/configuration work, the execution layer, native
session construction, producer and pump. Stop/disconnect/shutdown must join the
child and prove process cleanup before releasing the parent claim. Incomplete
cleanup retains that claim. Worker results are published only after cleanup.
The generic worker lane remains read-only until that ownership is released,
including after a terminal run row or failed cleanup. Owned worker session
tokens are also denied further delegation by the central spawn policy.

A conversation permits at most four concurrent host invocations. Copilot worker
admission is serialized through run-row creation so its requests respect the
configured per-creator cap. Generic delegation's existing count-then-create
race is outside this slice; the shared scheduler resource gate still applies.

`delegate_spawn` and `delegate_result` events commit before SSE delivery. Both
carry `tool_id`, `task_id`, `run_id`, `chat_id`, `agent`, and `name`. A result has
one terminal status (`completed`, `failed`, `cancelled`, `limit_exceeded`) and
at most 16,384 UTF-8 bytes of output. A hidden `delegation_request` audit frame
stores the invocation identity and argument digest before side effects. All
frames use the existing bounded personal conversation history.

The dashboard renders matching spawn/result identities once, includes saved
results, and marks archived unmatched spawns incomplete. Saved frames cannot
launch work or approve a prompt. Worker inspection opens its authorized run
route in a new tab so navigation does not unmount and cancel the parent.

## Qualification boundary

The follow-up [recovery contract](copilot-delegation-recovery-contract.md) adds
pre-dispatch worker identities, independent saved outcomes, archived refresh,
and restart quarantine. It also caps lifetime reservations at 32 per conversation.

See [results](copilot-delegation-results.md) for exact checks and live evidence.
This is a bounded coordinator-to-worker capability, not full CTO/repository/QA
acceptance. Real mixed-provider credentials, a complete multi-repository task,
proxy restart/disconnect recovery, shared Copilot payers, general engine routing,
remote workers, schedules and native terminal parity remain open.
