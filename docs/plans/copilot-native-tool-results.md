# Copilot native-tool policy results

This slice supplies a pre-execution policy gate and guarded session construction
for six pinned Linux tools: `bash`, `create`, `edit`, `view`, `glob`, and `grep`.
Copilot remains unregistered. This is a qualified subset, not full tool, MCP,
terminal, background-work or organization-workflow parity.

## Policy behavior

The native hook calls the same owned authorization method as the permission
bridge. Every operation requires the bound native session ID, trusted working
directory, current platform context, strict argument validation and an explicit
allow. Unknown tools, extra arguments, changed input and path rewrites deny.
The SDK normally catches hook exceptions and treats them as no decision; this
gate catches failure/cancellation and returns an explicit sanitized denial.

`create`/`edit` retain the exact path and content/string replacement; `view`
retains its path and read options. Searches check an explicit single root,
including the default working directory. Multi-root searches and glob syntax
that could escape the checked root are rejected. Bash checks the complete
command and pinned cwd, rejects shell-ID reuse, explicit async mode and
detachment, and bounds supplied wait values. **Sync Bash can still continue in
the background after its initial wait.** Native background ownership and
settlement must be qualified before enabling that execution route for users.

The stored structural schema fixture comes from the actual pinned runtime's
`tools.list` response. Guarded create/resume check the selected schemas before
opening a session. Metadata descriptions may change, but field types, required
arguments, enums and property structure must match. Unknown tools are not
implicitly admitted. The actual argument named `description` is preserved;
schema metadata with that name is treated separately.

## Session construction and the override finding

Native approval settings alone cannot implement OtoDock policy. The hook remains
active under native blanket/read approvals and manual permission mode. A
host-injected managed read-denial rule also remains effective when the platform
hook allows the operation.

Source-qualified `builtin:` filters help select the intended tools, but an SDK
custom tool with `overrides_built_in_tool=True` can still replace a built-in.
The hook sees the model-facing name, so it cannot distinguish that override.
The deterministic probe retains an unsafe raw-SDK negative control that executes
an inert overriding fixture to demonstrate this limitation.

The guarded `create_session` and `resume_session` methods therefore preserve
native-only tool configuration and reject protected option overrides before
any SDK access. Only a small explicit set of presentation/model/limit/managed
policy options is accepted. The methods install the hook and permission bridge,
disable discovery/file hooks/host git operations, validate the catalog, bind the
session identity before startup callbacks, check the returned identity, and
reject repeated or uncertain creation attempts. Resume never falls back to a
fresh conversation and forces `continue_pending_work=False`. Runtime startup and
cleanup remain the sandbox owner's responsibility.

Resume applies only to owned private history created under the same guarded
native-only profile. The pinned SDK omits empty external-tool and MCP
configuration from its resume payload; those values do not erase arbitrary
configuration saved by other profiles. The future account/session factory must
persist and verify profile provenance. Migration from custom/MCP/terminal
sessions is not qualified here.

## Evidence

The [deterministic sandbox probe](evidence/copilot-native-policy.json) supplies
no credentials and makes no model calls. It checks actual fixture files and
returned data for allow/deny across all six tools, native blanket approval,
automatic reads, manual mode without duplicate prompts, authority errors,
unknown custom tools marked `skip_permission`, injected managed denial, and
protected constructor overrides. Its raw overriding-tool case is explicitly an
unsafe negative control; guarded constructors reject the same input before SDK
access. Native direct-tool RPCs do not create resumable conversation history in
this pin, so its second runtime explicitly starts a fresh session.

The [model-driven sandbox probe](evidence/copilot-native-model.json) uses two
short authenticated turns and the guarded constructors. The model creates a
fixture when authorized; after runtime replacement and cold conversation
resume, a denied edit leaves that fixture unchanged even with native blanket
approval enabled. Both turns emit one DONE, and both runtimes close normally.
Policy answers in both probes are controlled fixtures. Offline integration
tests separately exercise the actual OtoDock authority, modes and path floors.
No deployed OtoDock service or real repository is changed by these probes.

## Remaining work

The six-tool gate does not qualify arbitrary native tools, shell background
control, subagent inheritance, web-fetch redirect policy, multi-root searches,
plans/skills/goals, custom or MCP tools, native permission-reply deduplication,
terminal policy, remote execution, or session-profile migration. Those remain
explicit parity work. The next local-engine integration must combine account
leases, owned profile/state records, catalog/model selection, this gate and the
supervisor; a selectable production engine is not introduced by this slice.

See the [pinned native contract](copilot-native-tool-contract.md) and
[full parity plan](copilot-parity.md).
