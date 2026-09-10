# Copilot agent chat navigation contract

The opt-in local Copilot preview is available from an agent's **Copilot chats**
sidebar section. **New Copilot chat** opens `/chat/:name/copilot`; selecting a
saved conversation opens `/chat/:name/copilot/:conversationId`. The settings
preview remains available and links to the same full page. Provisioning and
personal-account requirements follow the [preview setup](copilot-chat-preview-contract.md).

## Navigation and ownership

The agent in the route fixes the workspace. The first Send selects a personal
account, model and permission mode, creates the session, and replaces the new
chat URL with its conversation URL without interrupting that session. Reloading,
following a saved link, or using browser history only reads the saved transcript.
A cleanly closed conversation requires an explicit **Resume conversation** action
before another message can be sent. Its original account and configuration stay
fixed; no account substitution occurs.

The optional conversation parameter uses one route and panel instance. A real
conversation change invalidates pending UI work, aborts the stream, waits for
captured-owner cleanup, and then reads the target. Late create/resume responses
close the returned owner rather than attaching it to the newly selected page.
The settings link waits for an idle owner to close before entering its saved
route, so the destination cannot race that shutdown. It is unavailable while a
turn or owner operation is pending. User and agent changes replace the keyed
panel. Existing failed-cleanup fences
continue to block replacement work within that panel.

The route retains human authentication, password/2FA enrollment requirements,
and agent access checks. It is outside the generic subscription setup guard,
which only recognizes the existing registered engines. Settings already has an
exception to that guard, so its link provides an entry for Copilot-only members.
The Copilot page checks preview availability and eligible personal accounts;
this does not mark the user's generic engine setup as complete.

## Agent-bound history API

The existing list, detail and resume endpoints accept an optional `agent` query
parameter. The routed page always supplies it; the settings preview may omit it
to browse all of the user's accessible conversations.

- List authorizes the requested agent even when its history is empty. SQL
  filters by user and agent before ordering, offset and limit. The `has_more`
  probe uses the same filter.
- Detail and resume return 404 for an owner or agent mismatch before loading
  transcript events, checking native readiness, claiming a generation or
  starting a runtime. A route cannot override a conversation's saved agent.
- Resume still requires the saved revision and returns a fresh control handle.
  Existing cookie, origin, bounds and no-store rules remain in force.

Sidebar query keys include the signed-in user, agent and offset. Pagination is
20 conversations per page. Copilot rows navigate their explicit routes; regular
rows retain regular chat actions. Copilot history has no generic rename, move,
delete, warm-up or task behavior.

## Shared rendering

Both settings and routed chat use the shared message renderer for Markdown,
code blocks, copying, tool activity, permission prompts and structured questions.
The adapter correlates tool inputs/results by exact tool identifier within a
turn and preserves tool-start order. Unpaired events remain visible; a missing
completion is not presented as a successful tool result. Saved or retired
questions and approvals render as inert literal content, including after resume.

No generic chat identifier or file context is supplied. Copilot disables speech
and inline Markdown images, and shadows outer file context, so rendering cannot
start unqualified audio, file-preview or image requests. The shared renderer's
existing defaults remain enabled for other engines.

## Remaining qualification

This slice supplies agent navigation and shared presentation for the isolated
HTTP lifecycle. It does not register Copilot in the generic engine picker or
session manager. Generic chat rows, WebSocket/queue behavior, attachments,
artifacts, speech, model discovery/defaults, shared payers, graceful controls,
MCP/delegation, remote and terminal sessions, and organization automation remain
open against the [parity matrix](copilot-parity.md).
