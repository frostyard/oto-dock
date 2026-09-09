# C1 SDK/runtime observations

Status: C1 in progress, observed 2026-09-09. These results establish specific
integration primitives. They do not complete a parity row or make Copilot
available in the dashboard. C2–C9 remain open.

The [source-contract inventory](copilot-compatibility.md) pins SDK 1.0.13,
runtime 1.0.83, and protocol 3. The SDK's runtime downloader verified release
checksums. The [sandbox observations](copilot-sandbox-results.md) record the
downloaded executable/native-library hashes and host details.

## Executed SDK probe

Environment: Linux x64, Python 3.13.5, isolated dependency environment, temporary
working directory and Copilot state. The explicit GitHub CLI OAuth identity was
used for the live test; account details and tokens are not in the report. The
model was `gpt-5-mini`. This probe ran **outside** the OtoDock sandbox, with no
tools available for the first two prompts and only `create` available for the
denial prompt. Every tool permission was rejected.

| Check | Result |
| --- | --- |
| Runtime status and ping | Passed: runtime 1.0.83, protocol 3. |
| No-auth startup | Passed with isolated environment, keychain disabled, and stored-login fallback disabled. Runtime reported unauthenticated. |
| Explicit user authentication | Passed; model discovery returned the selected account's catalog. This does not establish organization installation-token support. |
| First inference | Passed; returned the exact requested marker. |
| Cold resume | Passed after disconnecting and stopping the first runtime, creating a new runtime, and resuming the durable session ID. |
| History continuity | Passed; the restarted runtime recalled the previous marker without receiving the marker in the second prompt. |
| Native file permission callback | Passed; a write request for the intended temporary file reached the callback, was denied, and the file was not created. No shell, MCP, or terminal permission equivalence is implied. |
| SDK through the real sandbox | Passed separately: one no-tool authenticated marker turn through `SandboxBuilder` and `oto-sandbox-net` in 8.142 seconds; existing temporary resolver preparation provided working DNS. See the sandbox report. |
| SDK shutdown | Passed; no observed tracked descendants required forced cleanup. |
| Idle sample | Recorded 0.00–0.01 CPU cores across two-second samples of tracked processes. This is a short observation, not a resource soak or fleet limit. |

The probe fed serialized live events through the initial translator without any
mapping exceptions. The report counted text, reasoning, and the denied native
tool's start/result events. Counts vary with tokenization and delivery. A later
offline-tested change limits unmapped diagnostics to one per type/session and
suppresses redundant byte-progress/partial-input frames.

Sanitized evidence: [SDK live](evidence/copilot-sdk-live.json),
[SDK no-auth](evidence/copilot-sdk-offline.json), and
[SDK through the sandbox](evidence/copilot-sandbox-sdk.json).
Both SDK recordings include translator counters. The live recording precedes
diagnostic throttling; the final no-auth recording includes it. These are narrow
observations, not a complete lifecycle test.

The separate [MCP report](copilot-mcp-results.md) records functional stdio
integration and the observed inference-token inheritance issue. A subsequent
live run through OtoDock's existing interceptor removed all three Copilot token
variables while preserving the tool call. An offline test verifies that this
does not remove separately authorized repository credentials. Production config
still needs to apply this boundary to every applicable MCP launch; environment
stripping alone does not prove process-information or filesystem isolation.

## Discoveries that change implementation details

- Released SDK constructors use keyword arguments. Some older auth examples
  still use a dictionary constructor. Use the pinned release, not copied examples.
- `mode="empty"` requires an explicit `available_tools` list. It is useful for
  controlled discovery; features must be enabled deliberately rather than
  inheriting an operator's CLI configuration.
- A session created without a turn did not yield resumable conversation history.
  The no-auth probe therefore checks startup/creation, while resume is checked by
  the live probe after a real turn. A created session ID alone is insufficient
  evidence for OtoDock's `can_resume_session`.
- The minimum accepted native session limit was **30 AI credits**. A one-credit
  cap failed at creation. OtoDock's token/time budget semantics need their own
  accounting and enforcement; native credits cannot be relabeled as tokens.
- The SDK enum classified vendor `model.*` frames as `unknown`, while retaining
  their names in `raw_type` and payloads in `to_dict()`. The translator preserves
  the type for diagnostics and discards unhandled payloads. It must not persist
  opaque vendor telemetry containing potentially sensitive prompt context.
- Raw `session.idle` is only a candidate completion. The translator emits `DONE`
  only after the future supervisor explicitly confirms settlement and the idle
  observation is still current. This is important for background agents/tools.

## First implementation artifact

[The pure translator](../../proxy/core/layers/copilot/translator.py) handles
streamed/final text and reasoning, tool identity/result pairing, errors, and
reconciled idle boundaries. It filters child-owned events from the main chat.
Unknown event types produce payload-free diagnostics. It is not registered with
the platform and adds no production SDK dependency.

Offline tests cover duplicate/reordered events, conflicting snapshots, tool
results arriving before starts, stale idle observations, duplicate completions,
and credential environment isolation. The transcript replay state is in memory;
durable replay and bounded retention remain session-layer work.

## C1 still required

- Full SDK tool/policy and persistence flows inside the actual sandbox; the
  combined startup/DNS/no-tool inference path is now proven.
- Stdio/HTTP MCP, credential broker integration, question/permission recovery,
  and native tool/background enforcement beyond the single write denial.
- Native terminal/headless history handoff and single-writer ownership.
- Mid-turn controls, compaction, goals, background settlement, and cancellation
  tested with live runtime behavior rather than API declarations alone.
- Credential expiry/refresh, multi-account concurrency, organization entitlement,
  and all supported satellite operating systems.

See [probe instructions](../../scripts/copilot/README.md) to reproduce the narrow
tests. Full parity remains governed by [P01–P16](copilot-parity.md).
