# C1 Copilot stdio MCP probe

Observed 2026-09-09 using Python SDK **1.0.13**, subprocess runtime **1.0.83**,
and `gpt-5-mini` on the Linux development host. This is a narrow SDK integration
probe outside the OtoDock sandbox; no production engine registration or service
configuration changed.

## Scope and controls

`scripts/copilot/mcp_probe.py` opts into one live user turn per invocation.
It gets the explicitly selected GitHub CLI token in memory with captured
subprocess output, supplies that token through the SDK, and disables stored-login
fallback. The runtime receives an explicit small environment, an isolated home,
an empty temporary workspace, and private temporary Copilot state. No repository
files or credential values are included in prompts or reports.

The SDK runs in `mode="empty"` with configuration discovery, file hooks, host git
operations, and persistent session storage disabled. The final probe exposes
only `ToolSet().add_mcp("oto-fixture-marker")`. The configured server exports one
no-argument tool returning `OTO_MCP_FIXTURE_OK`; it has no shell, file, or network
operation. Its fixture audit file accepts only a fixed call marker and token-name
presence booleans, then is removed with the temporary directory.

The permission callback checks the exact fixture server, expected tool identity,
and empty arguments before returning typed `PermissionDecisionApproveOnce`.
Everything else returns typed `PermissionDecisionReject`. Success requires
exactly one permission approval, one audited tool call, no other requested tool
permission, the correct final marker, and cleanup of all tracked descendants.

The script bounds overall time to 90 seconds by default (maximum 120), with a
60-second turn timeout. The runtime's minimum accepted session limit is 30 AI
credits; this is not a claim that one short turn consumes that amount. Five
short user turns were used to establish the final configuration; no automatic
retry loop exists.

## Findings

**Functional stdio MCP integration passed. Direct MCP launch leaked the SDK
inference token; wrapping the MCP with OtoDock's existing interceptor removed
it and passed the same live tool test.** The fixture only tested whether each
variable was nonempty; it did not return, log, or persist any credential value.

The first run failed closed: its callback expected the bare tool name `marker`,
but the runtime requested permission for `oto-fixture-marker`. The fixture did
not execute. Its original report recorded the denied counter and a missing audit
file error, without retaining the request shape. A second run classified the
request using fixed booleans and confirmed the namespaced tool identity, exact
server name, and empty object arguments. Updating that exact identity comparison
allowed one fixture call and returned its marker to the assistant.

The initial environment check covered only `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`,
and `GITHUB_TOKEN`; all were absent. Source review then identified the SDK's
actual user-token carrier, `COPILOT_SDK_AUTH_TOKEN`, plus its internal
`COPILOT_CONNECTION_TOKEN`. A final run explicitly checks all five names; the
earlier three-variable result cannot establish inference credential isolation.

Direct-launch environment check:

| Variable | Present in fixture environment |
| --- | --- |
| `COPILOT_SDK_AUTH_TOKEN` | **Yes** |
| `COPILOT_CONNECTION_TOKEN` | No |
| `COPILOT_GITHUB_TOKEN` | No |
| `GH_TOKEN` | No |
| `GITHUB_TOKEN` | No |

The SDK's `CopilotClient._start_cli_server()` injects `github_token` as
`COPILOT_SDK_AUTH_TOKEN` after constructing the caller-supplied environment. A
small explicit runtime environment therefore does not keep the inference token
out of MCP children by itself. **C2/C4 must provide and test a per-server launch
boundary that removes SDK/runtime inference credentials and exposes only that
MCP's broker-authorized credentials.** The wrapped probe below verifies a fix
candidate using existing code. Production wiring and regression coverage for
every MCP launch path remain release requirements.

The probe does not establish whether a child can additionally read runtime state
or process information. Installation-token authentication through runtime
environment variables also needs independent tests. OtoDock's credential broker
and sandbox isolation remain necessary.

Sanitized reports are retained as
[initial failed-closed attempt](evidence/copilot-mcp-initial.json) and
[direct-launch result with token-presence evidence](evidence/copilot-mcp-final.json).
The report's top-level `result` describes functional MCP assertions; the
token-presence evidence must also be reviewed for credential isolation.

## Existing interceptor fix candidate

The `--otodock-interceptor` option launches the real
[`proxy/core/stdio_path_interceptor.py`](../../proxy/core/stdio_path_interceptor.py)
as the MCP process. That process launches the unchanged fixture as its child.
The MCP configuration sets:

```text
OTO_STRIP_KEYS=COPILOT_SDK_AUTH_TOKEN,COPILOT_CONNECTION_TOKEN,COPILOT_GITHUB_TOKEN
```

The interceptor's existing `_apply_broker_credentials()` removes those names
before launching the actual MCP, including when no broker fetch token is set.
No runtime or interceptor code was changed for the probe, and the fixture does
not scrub its own environment.

The [wrapped live report](evidence/copilot-mcp-interceptor.json) passed with one
permission approval, one MCP call, the correct marker, all five observed token
names absent, and normal SDK cleanup without forced termination. The probe
asserts that all three `COPILOT_*` credential keys are absent and rejects wrapped
success if cleanup needs the fallback process killer.

The offline subprocess test also launches this real interceptor with sentinel
values: all three Copilot token variables are removed while `GH_TOKEN` and
`GITHUB_TOKEN` remain available. Those names can represent separately authorized
repository credentials; this candidate deliberately preserves their existing
semantics. Production must supply those only from the correct per-server
credential scope, rather than inherit ambient values.

This is an environment-inheritance fix candidate, not a security boundary
against a malicious process reading `/proc`, its parent, shared state, or other
same-user resources. The interceptor itself still receives the runtime token.
Production configuration must wrap every relevant MCP path, including satellite
launches, and separately verify process/filesystem isolation and broker behavior.

## Reproduce

Install the pinned `scripts/copilot/requirements.txt` into an isolated Python
environment and provision the matching verified runtime. With an eligible
GitHub CLI identity explicitly selected:

```bash
python scripts/copilot/mcp_probe.py \
  --runtime /path/to/copilot-runtime \
  --live --use-gh-token --output /tmp/copilot-mcp-result.json
```

Add `--otodock-interceptor` to test the candidate that strips inference-token
environment variables before the fixture starts.

Offline fixture and permission tests need the ordinary test dependencies plus
`psutil`; they do not need the SDK, a runtime, PostgreSQL, or inference tokens:

```bash
python -m pytest scripts/copilot/tests/test_mcp_fixture.py -q
```

The eleven tests verify stdio initialize/list/call behavior, notification handling,
secret-value redaction, refusal of unexpected tools/arguments, and the exact
permission boundary, including real interceptor stripping with repository tokens
preserved. HTTP MCP, MCP authentication renewal, elicitation, sandboxed
MCP startup, remote workers, and runtime failure recovery remain unverified by
this probe.

## Configuration follow-up

The session-contract branch adds
[`wrap_stdio_servers`](../../proxy/core/layers/copilot/mcp_config.py), now used
by the wrapped live probe. It always wraps explicitly configured stdio servers,
including those with no broker/path marker. It merges existing strip lists,
removes inference-token entries from the resulting config, preserves per-tool
filters and separately selected repository credentials, and leaves the source
config unmodified. Reapplying it does not nest the same interceptor. HTTP/SSE
and malformed configurations fail explicitly; HTTP policy support remains open.

The [recorded live result](evidence/copilot-mcp-config.json) passed one exact
fixture call, one permission approval, all three inference-token variables
absent in the child, and ordinary runtime cleanup. The live stream also passes
through the new completion coordinator: fresh task, permission, input-queue,
and processing snapshots yield one DONE, with zero replayed DONEs or mapping
errors. Offline tests also verify
stripping after broker injection and preservation of a brokered repository
token. This is configuration and interceptor evidence, not a live Oto broker
integration test or production engine registration.

Review exposed an existing interceptor bug: a case-insensitive removal deleted
only the first spelling of an environment key. POSIX permits multiple spellings
at once. The interceptor now removes every variant while preferring the exact
name's value when a return value is needed. The source was synchronized into
the satellite with `scripts/sync-satellite-code.sh`, including its integrity
hash. This shared fix applies to existing engines as well.

Independent review also reproduced a broker bundle replacing `OTO_STRIP_KEYS`
or reinjecting `OTO_MCP_FETCH_TOKEN`. The interceptor now captures the launcher's
strip policy before merging credentials and refuses those reserved keys from
the bundle, including case variants. Regression tests prove that broker-injected
inference credentials are removed while an authorized repository token survives.
The live fixture does not use a broker. The added broker regression uses
controlled in-memory bundles.
