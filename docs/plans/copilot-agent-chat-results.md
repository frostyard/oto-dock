# Copilot agent chat navigation results

Copilot now has agent-scoped chat URLs and a separate history section in the
regular chat sidebar. Settings links to the routed page, including the selected
saved conversation. Both surfaces share the existing Markdown, code, tool,
approval and question renderer. First-send URL replacement preserves the active
owner; following a saved URL remains read-only until explicit resume.

## Deterministic validation

The focused backend suite passes **190 API/service tests**. It covers optional
agent-query forwarding and bounds, filtering before pagination, authorization of
empty agent pages, and wrong-agent reads/resume rejected before transcript
access or generation/runtime changes. **42 storage validation tests** pass
locally; the storage suite also contains **37 PostgreSQL cases**, including the
new owner-and-agent filter test, for full CI.

Routed dashboard tests exercise first-create URL replacement, saved deep links,
explicit resume with the fresh handle, wrong-agent metadata, late owner cleanup,
rapid navigation during close, settings discovery, and authentication/enrollment
gates outside generic subscription setup. Sidebar tests cover availability,
user/agent cache isolation, pagination and explicit navigation. Actual shared
renderer tests cover Markdown/code, exact tool correlation, live controls, inert
saved prompts, missing completions and disabled file/audio/image behavior.
The full dashboard suite passes **883 tests across 128 files**. TypeScript and
the production build pass.

**1,107 offline Copilot tests plus 153 subtests**, repository-wide Ruff and diff
checks pass. Independent reviews examined backend authorization/pagination and
routed session ownership. Full regression and PostgreSQL results are recorded
on the PR after CI; no local PostgreSQL execution is claimed.

## Live evidence

The [HTTP probe](evidence/copilot-agent-chat.json) passed in **59.674 seconds**,
using SDK **1.0.13** and runtime **1.0.83**, with at most three model turns:

1. A native file creation required approval of the exact expected temporary path
   and contents through signed-cookie HTTP. Saved agent-bound list/detail reads
   left the closed runtime stopped.
2. Detail and resume requests naming a different agent returned 404 without
   changing the saved row or starting a runtime. Another administrator could
   neither read nor control the original user's conversation.
3. After replacing the application service and execution layer, agent-bound
   resume recalled the first turn using a fresh public handle. Stale revision
   and old-handle cleanup could not affect the resumed runtime.
4. Disconnecting a partial third turn left readable but nonresumable history.
   Revoking agent access also closed a separate idle session without inference.

All three runtimes exited normally, with every ownership claim and security
context released. The probe exercises real cookie/HTTP/SSE, configuration,
permission authority, sandboxing and private native history. User, agent and
account reads are controlled fixtures, and conversation storage is an explicit
in-memory seam. It is not a browser, full proxy-process restart, live PostgreSQL
or reverse-proxy deployment qualification.

## Remaining scope

The [route and renderer contract](copilot-agent-chat-contract.md) documents the
isolated HTTP lifecycle and available controls. Generic engine registration,
attachments and artifacts, queued input, speech, model/default integration,
shared payers, MCP/delegation, remote/terminal workflows and organization
automation remain open against P01–P16. No running deployment was changed.
