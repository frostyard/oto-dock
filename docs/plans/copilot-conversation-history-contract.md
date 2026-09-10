# Copilot saved conversations and explicit resume

The enabled local preview now persists conversations in PostgreSQL and exposes
an owner-only saved list in **User Settings → AI Engines**. Selecting an item
reads its transcript without inference. Resume is a separate action using the
stored agent, personal account, model and permission mode. No fallback account,
new-history replacement or automatic replay occurs if resume fails.

The existing [preview setup](copilot-chat-preview-contract.md) applies. Startup
adds two isolated tables through the existing idempotent schema initializer;
no existing chat rows or columns are changed. Reading saved history currently
requires the preview service to be enabled. Preexisting native-only preview
records are not imported: they lack the new ownership/turn ledger and transcript.

## Durable identity and events

`copilot_conversations` stores the original human owner, immutable selections,
private platform session ID, active generation, revision, timestamps and turn
state. `copilot_conversation_events` stores ordered flat event payloads with
positive sequence numbers. Neither table enters generic chat lists, search,
warmup, transcript seeding, title generation or retention.

The user message commits before native submission. Supported text, tool and
permission/question events commit before delivery; a successful terminal event
commits only after native DONE and producer exhaustion. A failed write cannot
produce a successful completion. Interrupted conversations retain their committed
partial output as read-only history. Stored prompts are inert when displayed;
they cannot submit decisions to an old or replacement runtime.

Each mutation locks the conversation row, checks the original owner and current
generation, and atomically updates its event sequence, byte count and revision.
Two simultaneous resume requests with one revision cannot both claim the row.
An old generation cannot append events or finalize the replacement's history.

Conversation IDs and active control handles are distinct. A cold resume uses
the same private platform/native history identity but issues a **fresh public
session handle**. Turn, permission, question and Close requests use that handle.
A delayed Close from an old tab therefore cannot close a resumed runtime.

## Reading and resuming

Every list/read is scoped to the original human and rechecks current agent
access. Another administrator cannot read or resume that person's conversations.
Disconnected accounts do not erase readable history; resuming still requires
the original account to be currently eligible. Deleting a user or agent follows
the database's existing cascade policy for its isolated transcript rows.

`can_resume` is a candidate indication: the row is cleanly closed with a completed
turn and the private native record passes read-only READY checks. Resume rebuilds
current configuration from stored selections and current account/agent authority.
The factory's exclusive record lock and exact profile/allocation comparison are
the final authority. Account, principal, tools, model, scope, instructions or
policy changes can still reject a candidate. Credential revision alone does not
change the same principal's durable profile.

Permission mode now participates in the native configuration digest. Older
digests fail exact matching; there is no silent profile upgrade or migration of
an uncertain history. Missing, altered, busy or ACTIVE records never fall back
to creating a new native session under the saved identity.

Open, interrupted or failed-cleanup rows cannot cold resume. A process crash
leaves its open row unavailable instead of automatically replaying pending work.
Failed or missing terminal delivery is treated conservatively even if native
work completed. Explicit Close after a clean observed turn can make a history
eligible; Stop during a turn does not promise resumability.

## HTTP contract

These routes use the preview's human-cookie authentication and sanitized errors.
History responses and newly opened owner responses send `Cache-Control: no-store`.
Resume additionally requires the same-origin JSON mutation checks.

| Method/path under `/v1/copilot/chat` | Response |
| --- | --- |
| `GET /conversations?limit=20&offset=0` | `{conversations, has_more}`; limit 1–100, offset 0–10,000. |
| `GET /conversations/{id}` | `{conversation, events}`; bounded ordered transcript. |
| `POST /conversations/{id}/resume` with `{revision}` | `201 {session_id, conversation_id}` for the new owner. |
| `POST /sessions` | Also returns `conversation_id` with the initial control handle. |

Public metadata contains ID, agent, account ID, model, permission mode, title,
timestamps, state, revision, `can_resume` and a static reason. It excludes native
IDs, runtime paths, credentials, internal generations and profile digests.
The page shows 20 conversations at a time. History reads have a 15-second total
HTTP budget so per-row authorization cannot make a page wait indefinitely.

Database work uses the dedicated executor. Conversation transactions set local
five-second statement and lock timeouts without changing pooled defaults. The
service waits up to 40 seconds for a mutation; a write that remains unsettled
stays attached to its generation. Cleanup drains pending writes for up to five
seconds, then retains the blocked owner instead of releasing capacity or allowing
a conflicting resume. These are operation bounds, not a promise that a broken
database connection has stopped executing.

Each saved conversation is limited to 1,000 events and 1 MiB of compact UTF-8
payloads, with a 256 KiB event maximum. User messages remain limited to 64 KiB.
Sequence numbers and JSON array framing have separate bounded browser headroom;
they do not reduce the accepted content budget. The existing session, turn and
idle bounds continue to apply.

## Remaining work

There is no deletion or automatic garbage collection of these transcripts or
private native history in this slice. Per-conversation bounds do not establish
an organization-wide storage quota. General chat integration, attachments,
queued input, graceful steering, shared payers, MCP/delegation, remote/terminal
workflows and organization automation remain gates in the
[full parity plan](copilot-parity.md). See the
[results and qualification limits](copilot-conversation-history-results.md).
