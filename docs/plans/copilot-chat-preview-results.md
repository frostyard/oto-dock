# Copilot authenticated chat preview results

The opt-in AI Engines settings preview now runs local Copilot conversations
through an authenticated HTTP API. It composes the provisioned execution layer,
current agent authorization, an explicitly selected personal payer, bounded
streaming, native permission/question responses and owned cleanup. The general
engine picker and main chat/task routing remain unchanged.

## Validation

**277 focused backend tests** pass: 79 API, 42 service, 7 application-lifetime,
72 authenticated builder, 72 execution-layer and 5 provisioned-layer tests.
Coverage includes two-user isolation, current access removal, strict origin and
cookie authentication, sanitized errors, concurrent admission, bounded turns and
queues, duplicate/wrong-session approvals, structured question validation,
disconnect before iteration and during terminal delivery, cancelled startup,
repeated cancellation, idle/death cleanup and retained ownership on failure.
Replacement contexts and foreign session claims retain their capacity reservations.

**31 focused dashboard/account tests**, TypeScript and the production build pass.
The dashboard checks split SSE framing, malformed/truncated replies, final-frame
and EOF requirements, tool approvals and questions, configuration selection,
single-flight sends/cleanup, Stop during creation, late session disposal,
unmount cleanup and cumulative display bounds.

**1,100 offline Copilot tests plus 153 subtests** pass. Repository-wide Ruff and
diff checks pass. Full proxy/PostgreSQL, audio, dashboard, satellite and optional
SDK compatibility results are recorded on the PR.

## Live HTTP evidence

The [sanitized HTTP report](evidence/copilot-chat-preview.json) passed in
**53.591 seconds** using SDK **1.0.13** and runtime **1.0.83**. The probe opened
a verified installation through the actual application lifetime and used real
signed session cookies, Uvicorn, HTTP SSE, the authenticated config builder,
shared capacity accounting, sandbox resolver and platform permission authority.

1. A cross-origin create request was rejected before runtime startup. A different
   administrator could not submit a turn to the owner's session.
2. Copilot requested one native file creation. The probe checked its exact path
   and content before approving through HTTP. The file contained the expected
   marker and the completed response reached the transport's final event.
3. A warm follow-up recalled the marker without tool execution.
4. Disconnecting during the third response closed the active owned runtime.
5. Removing the opening user's agent role closed a second, idle runtime through
   the service authorization watcher, without a further model call.

Both runtimes exited normally, with all contexts and ownership claims released.
The run used one actual GitHub token with controlled user, agent, credential and
network-discovery reads. It did not use PostgreSQL or a browser. Dashboard
interaction and native question submission have deterministic tests, not a live
browser/question recording. This does not establish actual OAuth refresh, two
real payers, reverse-proxy deployment or the organization acceptance scenario.

An initial probe attempt failed before any runtime launch because the isolated
HTTP fixture omitted shared capacity initialization. Its JWT creation also read
an uninitialized database setting. The fixture now initializes capacity and
supplies a fixed JWT-expiry setting; the run above passed with those corrections.

## Remaining integration work

See the [preview contract](copilot-chat-preview-contract.md) for operator setup,
limits and history behavior. Preview transcripts disappear from the page on
reload; private Copilot history remains on the server without automatic garbage
collection. Stop and disconnect close the session, and interrupted history is
not automatically resumed or replayed.

Main chat persistence, queued messages and attachments, authorized history
discovery/resume, full model/default selection, platform payers and generic
onboarding remain open. Graceful controls, MCP/delegation, remote and terminal
execution, automation and the complete P01–P16 parity matrix still require work.
This phase does not change the running installation on selfie.
