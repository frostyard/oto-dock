# Copilot conversation history and resume results

The local preview now saves ordered transcripts and immutable conversation
selections in isolated PostgreSQL tables. Users can reopen a saved transcript
without starting inference and explicitly resume a clean conversation with a
fresh control handle. The saved configuration and native record remain bound
to their original owner and payer.

## Deterministic validation

**345 focused backend tests** pass across the HTTP API, chat service,
application lifetime, authenticated config builder and execution layer.
They cover read authorization, stale revisions and handles, concurrent resume,
durable-before-delivery ordering, failed writes, cancelled/late commits,
bounded database waits and retained capacity during uncertain cleanup.

Terminal transport failures mark the captured owner as uncertain even after
the iterator has detached its completed turn. Missing final-frame or final-body
delivery therefore cannot advertise resume from a successful-looking transcript.
Read-only private-record checks do not create locks or mutate native state;
the exclusive factory open still performs final profile and writer checks.
Permission mode now participates in the native profile digest.

The new storage suite has **72 cases**: **36 validation tests** passed locally,
and 36 PostgreSQL cases exercise transactions, row locks, generation fencing,
atomic rollback, byte/event bounds, schema idempotence, account disconnection,
isolated cascades and transaction-local timeout reset. Those PostgreSQL cases
run in full CI; the local host's image policy rejected starting a temporary
PostgreSQL container, so no local database execution is claimed.

**47 focused dashboard/account tests**, TypeScript and the production build pass.
Tests cover saved-list paging, inert archived prompts, explicit resume,
unavailable saved accounts, late selection/metadata responses, stopped or
unmounted resume cleanup, and exact content limits with sequence/JSON framing
overhead. **1,107 offline Copilot tests plus 153 subtests** and repository-wide
Ruff/diff checks pass. Full regression results are recorded on the PR.

## Live evidence

The [HTTP history probe](evidence/copilot-conversation-history.json) passed in
**80.228 seconds**, using SDK **1.0.13** and runtime **1.0.83**:

1. A real native file creation required an exact checked approval through the
   signed-cookie HTTP API. The expected file contents and completed transcript
   were observed, and the runtime was cleanly closed.
2. Saved list/detail reads did not start inference. Another administrator could
   neither read the conversation nor find it in their own saved list.
3. The application chat service and provisioned execution layer were replaced.
   The same saved conversation resumed through the real private record and
   exclusive native history lock. A stale revision failed before runtime startup.
4. Resume returned a different public control handle. Deleting the old handle
   returned 404 and left the replacement runtime alive. Its next turn recalled
   the original file contents without tools.
5. Disconnecting a third, partially streamed response closed the runtime and
   left readable but nonresumable history. Idle role revocation also closed a
   separate unused session without another model turn.

All three runtimes exited normally; all contexts and ownership claims were
released. No forced process cleanup was required.

The probe uses actual HTTP, cookie authentication, configuration building,
permission authority, sandbox runtime, native history and resume checks. User,
agent and account reads are controlled fixtures, and conversation storage is an
explicit in-memory seam retained across service replacement. PostgreSQL
transaction behavior is tested separately. This is not a full proxy-process
restart, live browser, reverse-proxy deployment, OAuth refresh or two-payer test.
The final terminal-delivery fence has dedicated deterministic regressions; the
live report exercises an active partial disconnect.

## Scope and next gates

See the [saved-conversation contract](copilot-conversation-history-contract.md)
for API and storage behavior. Existing native-only histories are not imported.
Interrupted or crashed open rows remain unavailable for cold resume instead of
automatically replaying uncertain work. There is no transcript/native-history
deletion or automatic garbage collection in this slice.

Main chat routing, attachments, queues, generic model/default selection, shared
payers, graceful controls, MCP/delegation, remote/terminal workflows and
organization automation remain open against P01–P16. No running deployment was
changed.
