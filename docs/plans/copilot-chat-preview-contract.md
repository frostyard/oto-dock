# Copilot authenticated chat preview contract

The opt-in preview runs local Copilot conversations in **User Settings → AI
Engines → Copilot chat preview**. It supports streamed replies, native tool
activity, permission decisions and questions using an explicitly selected
personal GitHub account. It is separate from the regular chat list and does not
register Copilot in the general engine picker. This is an intermediate step
toward the [parity plan](copilot-parity.md), not full Claude/Codex parity.

## Operator setup

Follow the [local provisioning contract](copilot-local-provisioning-contract.md)
first: install the optional hashed SDK supplement into the proxy interpreter,
check dependencies, and initialize and verify the pinned runtime archive under
a private service-owned root outside agent workspaces and sandbox mount roots.
The qualified target is Linux x86_64 with glibc, SDK 1.0.13 and runtime 1.0.83.
The normal local sandbox prerequisites still apply.

Set `OTODOCK_COPILOT_LOCAL_ROOT` in the proxy service environment to that verified
installation root, then restart the proxy. For the provisioning guide's example,
the value is `/srv/otodock-copilot-private/local`. This setting does not download
or initialize an installation. An empty value leaves the preview disabled and
does not require the optional SDK. A configured but invalid installation fails
startup explicitly. The application owns the provisioned layer and closes the
preview before releasing that layer during shutdown.

Use a signed-in human dashboard account. Connect a GitHub account in the nearby
account section, enable its personal use, then select an accessible agent, that
account, a model ID and a permission mode. The model input starts at
`gpt-5-mini`; runtime preflight validates model availability. GitHub identity
validation alone does not establish Copilot entitlement or model access. This
preview does not borrow contributed platform accounts or choose a fallback payer.

## Conversation behavior

The first Send creates a fresh server session. Further sends use that session's
warm history. Configuration stays fixed until a new chat is started. The four
supported modes are `default`, `acceptEdits`, `plan` and `dontAsk`, using the
existing platform permission authority and path restrictions. The server fixes
the native tool profile to `bash`, `create`, `edit`, `view`, `glob` and `grep`;
MCP, custom tools and native shell control tools are not exposed.

The page displays response text and tool inputs/results, with controls for
matching permission and question requests. Permission approval is tied to the
current request and session; it is not a reusable blanket approval. Questions
use the captured question ID and offered choices/freeform policy. Duplicate,
stale, wrong-kind and wrong-owner responses are rejected. A request that closes
without an answer is not displayed as approved or answered.

Only one turn may run per session. There is no automatic message queue, stream
reconnection, replay or retry. Stop/Close terminates the entire session and joins
its owned runtime; it does not promise graceful continuation of an interrupted
turn. Losing an active stream also closes the session. The page aborts its
stream and attempts deletion on unmount or identity change. If creation returns
after the page has stopped waiting, it deletes the returned session instead of
forgetting that owner. New chat cannot bypass pending or failed cleanup.

Closing preserves the currently rendered transcript until New chat or page
reload. The preview does not persist transcripts in browser storage or create
regular chat-list entries. **Private Copilot history remains on the server**;
closing a runtime does not erase its history, and there is no automatic history
garbage collection in this preview. There is no history browser or resume API.

## Authentication, ownership and transport

The [HTTP routes](../../proxy/api/agents/copilot_chat.py) require human session
cookies, rejecting API keys, agent/session/external identities and Authorization
headers. Mutations require exact same-origin against the ASGI URL and JSON POST
bodies; a reverse proxy must provide the correct trusted scheme and host.

Under `/v1/copilot/chat`, authenticated `GET /status` returns `{available}`;
`POST /sessions` takes `{agent, account_id, model, permission_mode}` and returns
`{session_id}`. For that ID, `POST /sessions/{id}/turn` streams `{text}` replies;
`/permission` accepts `{request_id, approved}`, `/question` accepts
`{request_id, answers}`, and `DELETE /sessions/{id}` closes its owner.

The [service](../../proxy/services/engines/copilot_chat.py) restricts operations
to the opening user, including administrators, and rebuilds authorization before
turns and prompt replies. A five-second watcher checks current access/config and
runtime health, including idle sessions; changes close the owner. The watched
credential lease and live security context remain active. Revocation is polled,
not instantaneous. Startup/cleanup count toward capacity, uncertain cleanup
retains claims, and shutdown seals admissions before joining owners.

The final `turn_complete` frame requires one clean native DONE and producer
exhaustion. The browser requires that frame followed by clean EOF; native `done`
alone is insufficient. Truncated, malformed or trailing data and failed final
transport delivery trigger cleanup. Errors use sanitized messages.

## Bounds

Defaults are four sessions total, two per user, five minutes per turn (including
answers), five minutes idle and five seconds per authorization check. Shared
platform capacity may impose lower limits. Messages allow 64 KiB UTF-8; the page
also limits input to 32,768 characters. Each turn has a 128-frame queue and 1 MiB
serialized output cap. Browser parsing caps are 2 MiB, 5,000 frames and 262,144
characters per frame; cumulative display is capped at 1 MiB of characters and
1,000 events, then closes with a visible instruction to start a new chat. Native
sessions retain the 30-AI-credit limit; this is not a billing/entitlement promise.

## Evidence and remaining gates

[Preview results](copilot-chat-preview-results.md) distinguish deterministic
API/service/dashboard tests from the successful live HTTP proof. The latter
includes native file approval, a warm follow-up, disconnect cleanup and idle
role revocation, using controlled storage reads rather than PostgreSQL. It is
not a browser or reverse-proxy deployment qualification.

General engine registration and main chat persistence/routing remain open, as
do attachments, queued input, authorized history discovery and cold resume,
model catalog/default integration, shared payers and general onboarding.
Graceful steering/interrupt semantics, MCP/delegation, remote machines, terminal
sessions, meetings, phone workflows, scheduled work and organization automation
still require their own parity evidence. Enabling this preview does not make
those paths use Copilot or change an existing deployment automatically.
