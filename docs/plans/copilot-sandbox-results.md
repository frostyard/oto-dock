# C1 Copilot local sandbox probe

Observed 2026-09-09 on the development host, not selfie. This is a narrow
credential-free launch test, not completion of C1 or any parity row.

## Runtime and host

- Python SDK package selected by the compatibility spike: `github-copilot-sdk`
  **1.0.13**, whose published runtime pin is **1.0.83**. This probe talks directly
  over the runtime's stdio framing; it does not replace the planned SDK adapter.
- Runtime directory: `/tmp/otodock-copilot-runtime/prebuilds/linux-x64`.
- `copilot-runtime` SHA-256:
  `580f45a5dca10be9122180bce579055be25123efd348f8ed40ffd3e00aaf2044`.
- `runtime.node` SHA-256:
  `79f649256bdb76f448c6804cc7165eea3ba90b7773ace6758aeb77518125fbd8`.
- Linux kernel `7.1.8+deb13-amd64`, amd64; `/usr/bin/bwrap --version` reports
  `bubblewrap 0.12.0`. The package database reports
  `bubblewrap 0.11.0-2+deb13u1`, so the executable and package record differ.
  The passt package record is `0.0~git20250503.587980c-2+deb13u1`;
  `/usr/bin/pasta --version` produced no version text.

The probe uses the real `SandboxBuilder`, its role-derived workspace mounts,
capability drop, and the repository's `oto-sandbox-net` launcher. It supplies a
synthetic forward for port 1 because the builder requires a nonempty resolved
egress set. No proxy service or model endpoint is used. This does not exercise
the production egress resolver.

All writable application configuration and agent data is created under a fresh
temporary directory and removed afterward. The child receives a small explicit
environment, no tokens, no stored user home, and a private `COPILOT_HOME`.
The runtime receives `--no-auto-login` and `--no-auto-update`. No host settings,
services, credentials, or running installations are modified.

The runtime mount uses an actual `SandboxMount` at `/opt/copilot-runtime`, with
its source constrained to the configured runtime install tree. This places the
read-only mount **after** the sandbox's `/tmp` tmpfs; an identity bind of a `/tmp`
runtime path before that tmpfs would be hidden. Production runtime provisioning
still needs its own reviewed engine mount design.

## Results

| Check | Observed result |
| --- | --- |
| Real sandbox launch and stdio `ping` | Passed in 0.452 seconds; reply `pong: otodock-sandbox-probe`, protocol version 3. |
| Shared workspace write | Succeeded in the isolated agent workspace. |
| Knowledge write | Kernel denied with errno 30 (`EROFS`). |
| Runtime installation write | Kernel denied with errno 30 (`EROFS`). |
| Ordinary cleanup | Four descendants observed, zero live tracked descendants after cleanup. |
| Forced timeout with a live runtime | After a successful ping, intentionally held the process open; the two-second deadline killed the tree in 2.139 seconds. Exit signal was SIGKILL; four descendants observed, zero live tracked descendants after cleanup. |

Both initial runs emitted pasta's `Couldn't get any nameserver address` warning.
The namespace and local stdio exchange still worked. Those two tests did not
establish DNS or authenticated inference; the integrated test below subsequently
verified them using the existing resolver preparation.

The native `copilot-runtime` binary does not support `--version` or `--help`.
Launching with only `--stdio` also did not reach the ping. Using the SDK's runtime
launch options (`--headless --stdio --no-auto-update --no-auto-login --log-level
error`) succeeded. Runtime status must be queried through the protocol instead
of assuming it implements the native terminal CLI's flags.

## Reproduce

Use a Python environment containing the proxy dependencies, including `psutil`,
and a pre-provisioned, verified runtime directory:

```bash
/tmp/otodock-c0-ci-venv/bin/python scripts/copilot/sandbox_probe.py \
  --runtime-dir /tmp/otodock-copilot-runtime/prebuilds/linux-x64

/tmp/otodock-c0-ci-venv/bin/python scripts/copilot/sandbox_probe.py \
  --runtime-dir /tmp/otodock-copilot-runtime/prebuilds/linux-x64 \
  --exercise-timeout --timeout 2
```

The default deadline is ten seconds; the script restricts it to 1–30 seconds.
The forced-timeout variant exits successfully only if ping happened first, the
deadline fired, and no tracked live descendants remain. Cleanup uses a dedicated
process group plus recursively tracked child processes; it is a bounded probe
cleanup mechanism, not a claim that production cancellation is implemented.

## Remaining evidence

This proves a pinned runtime can start within the existing Linux sandbox and
answer a control request while read-only mounts stay enforced. Together with the
integrated result below, it also proves SDK startup and one authenticated model
turn inside that sandbox. It does not yet prove SDK permission callback
integration inside the sandbox, tool-level permission denial, MCP execution,
negative network-policy enforcement, session resume, terminal interoperability,
multi-account isolation, idle resource behavior, or macOS/Windows support. Each
remains required by the [parity plan](copilot-parity.md).

## Integrated SDK and sandbox inference

A subsequent bounded test on 2026-09-09 used
[`sandbox_sdk_probe.py`](../../scripts/copilot/sandbox_sdk_probe.py) to launch the
actual SDK through the actual sandbox, rather than combining conclusions from
separate transport and mount tests:

```text
CopilotClient / RuntimeConnection.for_stdio
  -> temporary process-group wrapper
  -> oto-sandbox-net (pasta + route policy)
  -> SandboxBuilder's bubblewrap command
  -> /opt/copilot-runtime/copilot-runtime
```

The wrapper calls `setsid()` before executing the sandbox launcher. The probe
tracks descendant processes throughout SDK startup, inference, and shutdown,
and cleans the dedicated process groups and tracked descendants afterward.
SDK flags are appended after the full sandbox command, so the native runtime
receives them inside bubblewrap. No in-process runtime or unsandboxed fallback
is used.

The host uses a systemd-resolved loopback stub. Calling the existing
`netns_preflight()` under isolated `PLATFORM_DATA_DIR` prepared
`sessions/netns-resolv.conf` and made the builder select pasta's DNS forwarder.
This used the existing production resolver handling; no host resolver, route,
firewall, or AppArmor policy was changed.

| Integrated check | Observed result |
| --- | --- |
| SDK/runtime/protocol | SDK 1.0.13, runtime 1.0.83, protocol 3; SDK startup inside the sandbox passed. |
| Authentication | Explicit `--live --use-gh-token`; token selected internally from GitHub CLI, passed through the SDK's token option. No token was printed in the report. |
| Model turn | One `gpt-5-mini` turn returned exactly `OTO_SANDBOX_SDK_OK`. |
| Tool policy for this test | Empty mode, no available tools, config/file-hook/host-git discovery disabled, reject callback installed; zero permission requests and zero tool executions observed. This is not an allow/deny policy test. |
| Runtime events | One assistant message, seven text deltas, one assistant turn end, and one session idle observed. Reports retain counts only, not raw protocol payloads. |
| Duration and cleanup | Entire invocation completed in 8.142 seconds; four descendants tracked; zero live tracked descendants after cleanup. |

The inference wait is capped at 45 seconds, the SDK work block at 60 seconds,
and SDK stop at five seconds followed by bounded force cleanup. Existing host
preflight and token lookup have their own subprocess timeouts. The runtime
requires a minimum 30-credit per-session limit; this probe sends only one short
turn and does not claim the provider enforces a smaller credit cap.

Reproduction requires proxy dependencies plus the pinned SDK in a disposable
environment and an explicitly selected eligible GitHub CLI account:

```bash
/tmp/otodock-c0-ci-venv/bin/python scripts/copilot/sandbox_sdk_probe.py \
  --runtime-dir /tmp/otodock-copilot-runtime/prebuilds/linux-x64 \
  --live --use-gh-token --output /tmp/otodock-sandbox-sdk-results.json
```

All application configuration, agent workspace, and runtime state were temporary
and removed after the run. The running deployment on selfie was not involved.
