# Copilot attached-shell ownership results

This slice adds an opt-in `CopilotNativeShellSession` adapter for one guarded
native-only session in an owned Linux runtime. Copilot remains unregistered.
The adapter combines strict native task snapshots with an independent runtime
process check; neither a tool result nor a cancellation acknowledgement can
prove that shell work has stopped.

## Findings and resulting behavior

The pinned runtime exposes several distinct boundaries:

- A synchronous shell remains visible in `tasks.list` while its tool RPC waits.
  Explicit promotion changes its execution mode to background and releases the
  pending tool call. The direct RPC did not automatically return after the
  supplied ten-second initial wait in the recorded case.
- `tasks.cancel` can report success and mark a shell terminal while its fixture
  process is still alive. Terminal task status is therefore insufficient.
- Natural synchronous completion removes the task from inventory. Missing task
  IDs alone cannot distinguish retirement from lost tracking.
- Task PIDs are inside the sandbox PID namespace. They are not host process IDs
  and are never used to signal host processes.
- Native abort can leave attached background work running. Direct interruption
  without an active model turn can return false. An accepted control is followed
  by explicit cancellation of observed native shell task IDs and fresh checks.
- Model-driven abort can omit `tool.execution_complete` after stopping a shell.
  The adapter supplies separate native-shell stop evidence; the supervisor only
  applies it to known open top-level `bash` calls under an accepted control and
  the existing completion fences. The resulting tool event reports cancellation,
  not success or absence of earlier side effects.

## Process and session ownership

Immediately after runtime startup, before creating any SDK session or admitting
work, the owner calls `capture_process_fence()`. The baseline can be captured
only once. The check binds to the same SDK client and one session ID/object,
uses the existing exact process owner, and compares host PID/start-time pairs.
Any additional live owned process blocks settlement, including a persistent
helper. Unrelated host children do not enter this ownership boundary.

The shell adapter requires this independent process check. It refreshes and
validates all native task metadata, rejects unsupported task kinds/detachment,
retains missing active tasks as unknown, and rejects identity reuse. Missing
previously owned tasks become host-only `RETIRED` only when the process check
passes. That state does not fabricate an exit code or successful tool result;
a native status string named `retired` remains unknown.

Before abort/interrupt, the supervisor pauses permission admissions. The adapter
captures current ownership, sends the native control, cancels exact observed
shell IDs if accepted, and waits within a deadline for both task and process
settlement. A false cancellation acknowledgement may reflect natural completion;
it still requires a fresh observation. Unknown, malformed, timed-out or uncertain
state invalidates the adapter and requires owned-runtime cleanup. A synthetic
process-pending entry never becomes a cancellation RPC target.

The SDK snapshot adapter also rejects malformed or duplicate task/permission
identities, malformed queue data and non-boolean processing/control results.
Unknown explicit task statuses block completion. Queue occupancy records no
private prompt text and is used together with native processing and event fences.

## Evidence

The [no-auth sandbox probe](evidence/copilot-native-shell.json) passed in
50.419 seconds and records actual
native task/control shapes, exact fixture process checks, natural task removal,
explicit promotion and guarded adapter behavior. It makes no model requests.
The guarded promoted shell's tool RPC returned while the process remained
alive; the fence blocked until adapter abort joined the fixture process.
Its direct tool RPC path does not emit the model loop's tool-start/completion
pair, so those observations are not model-turn settlement evidence.

The [model-driven probe](evidence/copilot-native-shell-model.json) passed in
76.232 seconds with bounded normal/abort/interrupt cases through the actual
sandbox runtime, native policy, shell adapter and supervisor. Each case emitted
one DONE with no live fixture process or pending host request at completion.
All three runtimes closed normally. Both controlled turns omitted the native
tool-completion event; the separate stop proof allowed cancellation to settle.
Offline tests exercise malformed
inventories, session replacement, process identity reuse, late descendants,
control uncertainty, native-shell stop evidence, and paused consumers.

## Scope and remaining gates

The process fence is a conservative census through the existing runtime owner,
not an atomic kernel process census or a guarantee for arbitrary daemonized
command descendants. Persistent new helpers can conservatively block completion.
The recorded fixtures establish attached-shell behavior and normal owned-runtime
cleanup; broader detached/subagent/restart behavior remains unqualified.

Model-facing `read_bash`, `list_bash`, `stop_bash`, async/detached launches and
shell-ID reuse remain denied by the native policy. The probe's explicit promotion
and raw controls are host-driven qualification operations, not newly exposed
product capabilities. The adapter admits only the guarded native-only profile;
account-bound construction must persist and verify that provenance for resume.
No selectable engine, deployment change, satellite support or terminal integration
is introduced here. These remain work toward the [full parity plan](copilot-parity.md).
