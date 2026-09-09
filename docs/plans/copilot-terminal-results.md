# C1 native terminal and SDK history handoff

Verified 2026-09-09 on Linux amd64, using SDK **1.0.13**, runtime **1.0.83**,
protocol **3**, and the separate full native CLI **1.0.83**. This is a passing
compatibility spike for sequential history sharing. It is not completion of
OtoDock terminal parity or permission enforcement.

## Executable provenance and isolation

The native CLI was downloaded from the official
[1.0.83 release](https://github.com/github/copilot-cli/releases/tag/v1.0.83), asset
`copilot-linux-x64.tar.gz`, and checked against the release's `SHA256SUMS.txt`:

```text
ffbe1c429664b8a05efed67ecdb467123e40fcaa3c6c14ef9a98ba74da4687b7
```

The probe additionally compares the bytes of the executable it will run against
the `copilot` archive member. A checksum-valid archive next to an unrelated
executable is rejected. The separate installation lives under
`/tmp/otodock-copilot-terminal-1.0.83`; no installed user CLI was replaced.

Each probe creates a fresh temporary `HOME`, `COPILOT_HOME`, workspace, and
configuration. Even `--version` runs with isolated environment/configuration.
The sole trusted folder is the disposable workspace. Auto-update, built-in MCPs,
hooks, custom instructions, memory, IDE auto-connect, and remote export are
disabled for the native session. No ambient credentials, provider endpoints,
`BASH_ENV`, or allow-all settings are inherited. The selected GitHub CLI token
is fetched internally and never written into reports or command arguments.

This probe runs on the development host **without the OtoDock sandbox**. It uses
empty available-tool settings and native shell/write denial rules, and checks
persisted history for zero tool executions. Those facts do not establish full
native permission parity; the SDK reject callback cannot enforce native PTY
actions after the SDK process has stopped.

## What actually ran

1. A fresh SDK/runtime session answered a short marker prompt and persisted its
   history under `COPILOT_HOME/session-state/<session-id>/events.jsonl`.
2. The SDK session disconnected, `client.stop()` completed, and the probe
   checked that no previous writer remained before launching the native CLI.
3. The real native CLI ran on a PTY with `--resume=<session-id> -i <prompt>`.
   This is interactive mode with an initial prompt, not the noninteractive
   `--prompt` mode. The native prompt did **not** contain the original marker;
   the model recalled it from SDK history and appended `-NATIVE`.
4. The native reply appeared in that same session's history file. The probe sent
   `/exit` through the PTY and required a clean exit before handing back ownership.
5. A new SDK/runtime process resumed the same ID and recalled the native reply,
   including its suffix, without receiving the marker in its new prompt.

No transcript conversion or synthetic terminal was used. The existing session
store worked in both directions for this no-tool conversation.

## Recorded result

The final probe passed in **20.152 seconds**. The sanitized machine-readable
result is [copilot-terminal-handoff.json](evidence/copilot-terminal-handoff.json).

| Check | Result |
| --- | --- |
| SDK initial turn | Passed. |
| Native recall of SDK history | Passed. |
| Actual terminal output | 5,527 bytes received; ANSI control sequences observed. Raw terminal output was not persisted. |
| Resize handling exercise | PTY resized from 120×35 to 100×40 and SIGWINCH sent; execution continued. This does not verify rendered layout. |
| Native clean exit | `/exit` returned exit code 0. |
| SDK recall of native history | Passed in a new SDK/runtime process. |
| Sequential writer ownership | Passed at every handoff; no simultaneous SDK/native writer. |
| Ordinary cleanup | Initial SDK, native CLI, and resumed SDK all stopped normally; no force cleanup was required. |
| Tool execution | Zero `tool.execution_start` records across the complete persisted history. |
| Remaining native processes | Zero live tracked descendants after cleanup. |

An initial launch attempt failed before native inference because
`--deny-tool=*` is an invalid native rule. This was reproduced without any
credentials. The accepted rules used in the passing run are
`--deny-tool=shell` and `--deny-tool=write`; this distinction must carry into the
eventual native launcher. The SDK's permission callback API and the native CLI's
rule syntax are different interfaces.

## Reproduce and limits

[`terminal_probe.py`](../../scripts/copilot/terminal_probe.py) requires explicit
live-account selection and an already verified separate native CLI download:

```bash
/tmp/otodock-copilot-spike-venv/bin/python scripts/copilot/terminal_probe.py \
  --runtime /tmp/otodock-copilot-runtime/prebuilds/linux-x64/copilot-runtime \
  --cli /tmp/otodock-copilot-terminal-1.0.83/extracted/copilot \
  --cli-archive /tmp/otodock-copilot-terminal-1.0.83/copilot-linux-x64.tar.gz \
  --live --use-gh-token --output /tmp/otodock-native-terminal-result.json
```

The passing run used three short `gpt-5-mini` turns. Each SDK phase has a
60-second work limit and a 45-second inference wait; the native phase has a
60-second limit. Each shutdown has a bounded grace period followed by force
cleanup if required. Requiring force cleanup makes the probe fail rather than
masking teardown problems. Session AI-credit limits use the runtime's minimum
supported value of 30 credits, not a claim of a smaller enforced cost cap.

Seven offline tests cover environment isolation, partially appended history,
corrupt history, child-message filtering, refusing overlapping writers, and
matching the actual CLI bytes to the verified archive. Ruff and these tests
passed.

Still unverified: native Linux execution inside OtoDock's sandbox, native
permission hooks and plan-mode write prevention, reconnect after proxy/browser
loss, unexpected CLI/runtime death, cancellation during a tool, remote PTYs,
Windows/macOS, Unicode input, attachments, visual rendering, and long-session
history/compaction behavior. The final implementation must enforce writer
ownership centrally; this probe's checks apply only to its own sequential
processes. P13 and P14 remain open.
