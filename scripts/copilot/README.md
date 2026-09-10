# Copilot compatibility probes

These are development probes for C1 of the
[parity plan](../../docs/plans/copilot-parity.md). They do not register an engine,
change an OtoDock installation, or establish full parity.

## Owned permission callbacks

With the proxy dependencies and pinned SDK installed, run the bounded Linux
sandbox permission probe:

```bash
python scripts/copilot/permission_probe.py \
  --runtime-dir /tmp/otodock-copilot-runtime/prebuilds/linux-x64 \
  --live --use-gh-token --output /tmp/copilot-permission-results.json
```

It submits three short turns using one inert trusted Python fixture: reject,
approve once, and abort while approval is held. The policy answers are controlled
fixtures; no dashboard user is impersonated. Host Python callbacks run outside
bubblewrap and must be trusted. The probe verifies native request retirement,
owned callback cleanup and exactly one turn completion, with a 150-second overall
deadline and the runtime's minimum 30-credit session ceiling. See
[permission results](../../docs/plans/copilot-permission-results.md) for the
scope and remaining every-tool policy gate.

## Isolated SDK environment

Use Python 3.13 for the recorded environment. The dependency lock includes the
published SDK **1.0.13**, which pins runtime **1.0.83** and protocol **3**.

```bash
python3 -m venv /tmp/otodock-copilot-probe-venv
/tmp/otodock-copilot-probe-venv/bin/python -m pip install --require-hashes -r scripts/copilot/requirements.txt
COPILOT_CLI_EXTRACT_DIR=/tmp/otodock-copilot-runtime /tmp/otodock-copilot-probe-venv/bin/python -m copilot download-runtime
```

The SDK downloader verifies the release checksums. Keep the complete staged
runtime tree, including `runtime.node`; do not copy just its executable. The
commands below use the Linux x64 staging path. Other architectures need the
matching path and have not been qualified by these results. The SDK transport
probe currently uses a POSIX child environment.

Regenerate the development lock with `uv pip compile
scripts/copilot/requirements.in --python-version 3.13 --generate-hashes
--output-file scripts/copilot/requirements.txt` (recorded with uv 0.12.12).
These dependencies are deliberately separate from platform requirements while
the production session/auth design is unimplemented.

## Transport, history, and permission callback

No credentials or inference by default:

```bash
/tmp/otodock-copilot-probe-venv/bin/python scripts/copilot/probe.py \
  --runtime /tmp/otodock-copilot-runtime/prebuilds/linux-x64/copilot-runtime \
  --output /tmp/copilot-offline-results.json
```

The live variant explicitly selects the current `gh` identity. The token stays
in memory and the runtime's environment; output contains counts and outcomes,
not tokens, account details, prompts, or raw protocol errors. It submits three
short turns to the selected account: a fixed marker, recall after runtime
restart, and a file creation which the permission callback rejects.

```bash
/tmp/otodock-copilot-probe-venv/bin/python scripts/copilot/probe.py \
  --runtime /tmp/otodock-copilot-runtime/prebuilds/linux-x64/copilot-runtime \
  --output /tmp/copilot-live-results.json --live --use-gh-token
```

The default model is `gpt-5-mini`; `--model` can select an available model. Each
send has a 45-second timeout and the full probe defaults to 180 seconds. Runtime
1.0.83 rejects native session caps below 30 AI credits; this is a ceiling rather
than an expected charge. External subprocesses get fresh temporary state and
an explicit environment. The script is a **transport/policy callback test outside
the OtoDock sandbox**; it exposes only the native `create` tool during the denial
turn and never approves a tool call. Cleanup checks tracked process identities,
with bounded forced cleanup on failure. It is not a production supervisor.

The event translator sees live serialized events and reports mapping failures.
It does not declare a turn settled: background/permission reconciliation belongs
to the session supervisor. Event counters preserve the original type of
vendor events unknown to the SDK enum.

## Actual Linux sandbox

`sandbox_probe.py` uses OtoDock's real `SandboxBuilder` and network launcher. Run
it in an environment with the proxy's dependencies installed. It creates only
temporary configuration and agent data, then tests a protocol ping, read-only
mount enforcement, and descendant cleanup. It needs bubblewrap and pasta on the
host, but does not modify host policy.

```bash
python scripts/copilot/sandbox_probe.py \
  --runtime-dir /tmp/otodock-copilot-runtime/prebuilds/linux-x64
python scripts/copilot/sandbox_probe.py \
  --runtime-dir /tmp/otodock-copilot-runtime/prebuilds/linux-x64 \
  --exercise-timeout --timeout 2
```

With both the proxy dependencies and the pinned SDK installed, test one
authenticated no-tool turn through the actual sandbox:

```bash
python scripts/copilot/sandbox_sdk_probe.py \
  --runtime-dir /tmp/otodock-copilot-runtime/prebuilds/linux-x64 \
  --output /tmp/copilot-sandbox-sdk-results.json --live --use-gh-token
```

This uses the existing resolver preflight with temporary application data and
configuration; it does not change host DNS or namespace policy.

## Stdio MCP boundary

The fixed-tool MCP probe selects only its bundled fixture and allows only that
tool's permission request. See [MCP results](../../docs/plans/copilot-mcp-results.md)
before interpreting its result: the raw SDK passes its inference token to the
MCP child. Functional success alone does not pass the credential-isolation gate.

```bash
python scripts/copilot/mcp_probe.py \
  --runtime /tmp/otodock-copilot-runtime/prebuilds/linux-x64/copilot-runtime \
  --live --use-gh-token --output /tmp/copilot-mcp-result.json
```

Add `--otodock-interceptor` to test the verified fix candidate using OtoDock's
existing wrapper. That variant additionally requires the Copilot inference and
connection-token variables to be absent from the fixture's environment. It does
not establish filesystem/process-information isolation or wire production MCPs.

The wrapped variant now uses the adapter's `wrap_stdio_servers` configuration
helper. It wraps every explicitly supplied stdio server, merges existing broker
strip lists, preserves tool filters and repository credentials, and rejects
unsupported transports. See the [configuration follow-up](../../docs/plans/copilot-mcp-results.md#configuration-follow-up).

## Owned callbacks and sandbox session supervisor

`callback_probe.py` isolates SDK-hosted callback cancellation and join. Its
[recorded results](../../docs/plans/copilot-callback-results.md) distinguish host
callback ownership from the native task registry:

```bash
python scripts/copilot/callback_probe.py \
  --runtime /tmp/otodock-copilot-runtime/prebuilds/linux-x64/copilot-runtime \
  --live --use-gh-token --output /tmp/copilot-callback-registry.json
```

`supervisor_probe.py` connects the source runtime owner, callback registry, SDK
adapter and supervisor through the real Linux sandbox. It requires proxy
dependencies, the pinned SDK, bubblewrap, pasta and Linux pidfd support:

```bash
python scripts/copilot/supervisor_probe.py \
  --runtime-dir /tmp/otodock-copilot-runtime/prebuilds/linux-x64 \
  --live --use-gh-token --output /tmp/copilot-supervisor.json
```

This selects the current GitHub CLI identity internally and submits three short
prompts: normal streaming, abort with an owned callback, and interrupt with an
owned callback plus controlled background-task metadata. The full work deadline
is 180 seconds. Native shell/subagent work is not exercised. The trusted callback
only waits in host Python and has no external side effects; the native runtime
runs inside the sandbox. See the [supervisor results](../../docs/plans/copilot-supervisor-results.md)
for exact guarantees, the runtime's minimum 30-credit ceiling, and remaining gates.

## Scoped credentials and runtime replacement

`account_probe.py` uses typed credentials, private state and the account observer
with two sequential real sandbox runtimes. It requires the same Linux/proxy/SDK
setup as the supervisor probe:

```bash
python scripts/copilot/account_probe.py \
  --runtime-dir /tmp/otodock-copilot-runtime/prebuilds/linux-x64 \
  --live --use-gh-token --output /tmp/copilot-account-restart.json
```

The probe uses at most two short prompts and an in-memory credential source.
It selects the same GitHub CLI user token for both generations, checks sequential
history resume, changes the observed generation to trigger independent cleanup,
and checks that a no-auth runtime cannot inherit authentication from retained
state. This does not mint or refresh a real OAuth/installation token or prove two
real payer accounts. See the [authentication contract](../../docs/plans/copilot-auth-contract.md)
and [account foundation](../../docs/plans/copilot-account-leases.md).

## Offline regression tests

The [native terminal probe](../../docs/plans/copilot-terminal-results.md) runs a
real SDK → PTY → SDK history round trip with a separately verified full CLI.
The [lifecycle probe](../../docs/plans/copilot-lifecycle-results.md) exercises
abort, interrupt, immediate input, compaction, and the task registry in separate
bounded scenarios. Both require explicit live-account selection; their reports
record limitations as well as passing checks.

The MCP probe also routes real events through the
[completion coordinator](../../docs/plans/copilot-session-contract.md) and checks
that fresh runtime snapshots produce one completion and no replayed completion.

With the repository's test dependencies and `psutil` installed:

```bash
python -m pytest scripts/copilot/tests -q
```

These tests require neither inference credentials nor PostgreSQL. They cover
credential isolation, adverse event ordering, callback ownership, control and
consumer races, SDK snapshot adaptation, and exact runtime process cleanup.
The account-store tests in `proxy/tests/billing/test_copilot_accounts.py` run in
the PostgreSQL-backed proxy suite, with fake token values and no inference.
The runtime tests use fake SDK transport and real disposable processes; the SDK
is not installed by CI.
See the [recorded results](../../docs/plans/copilot-spike-results.md) for what has
actually been run and what remains open.
