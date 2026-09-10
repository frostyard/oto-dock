# Pinned native tools and pre-tool-use policy

Target: **github-copilot-sdk 1.0.13**, **Copilot native runtime 1.0.83**,
protocol 3. This audit separates observed tool metadata, inspected SDK behavior,
and native execution coverage that requires its own regression evidence.

## Observed catalog

A credential-free runtime was started through the existing mandatory OtoDock
Linux sandbox. No model request or tool execution was sent. Both
`client.rpc.tools.list(ToolsListRequest())` and the same call with
`model="gpt-5-mini"` succeeded and returned identical JSON with 15 tools.
The owner closed normally with `forced_cleanup=False`.

The complete local diagnostic is `/tmp/copilot-native-catalog.json`; it contains
public tool metadata only. The canonical JSON of the gpt-5-mini `tools.list`
result, serialized with sorted keys and compact separators, has SHA-256
`b9ffd08856b1388afa644d7cb2a7c9a8fac0aa60ac0865cf5ec64a674fa2a215`.
The table below records the relevant schema so the contract does not depend on
that temporary artifact surviving.

The reviewed six-tool structural fixture is
[`native_tool_schemas.json`](../../proxy/core/layers/copilot/native_tool_schemas.json),
formatted as `{native_name: parameters_schema}` for bash/create/edit/view/glob/grep.
Only descriptive metadata is removed; types, enums, required arrays, properties,
and their nesting remain. Normalization must preserve property names: bash's
actual argument named `description` remains `properties.description`, while
its explanatory `description` annotation is removed. Blindly deleting every
dictionary key named `description` would corrupt the schema.

These are model catalog candidates, **not** proof of a particular session's
offered tool set. `available_tools` must still explicitly select the intended
tools, and session initialization/metadata must confirm what is offered.

| Native name | Required JSON arguments | Optional JSON arguments |
|---|---|---|
| `bash` | `command: string`, `description: string` | `shellId: string`, `mode: "sync" | "async"`, `detach: boolean`, `initial_wait: number` |
| `read_bash` | `shellId: string`, `delay: number` | none |
| `stop_bash` | `shellId: string` | none |
| `list_bash` | none; object input | none |
| `view` | `path: string` | `view_range: integer[]`, `forceReadLargeFiles: boolean` |
| `create` | `path: string`, `file_text: string` | none |
| `edit` | `path: string` | `old_str: string`, `new_str: string` |
| `glob` | `pattern: string` | `paths: string | string[]` |
| `grep` | `pattern: string` | `paths: string | string[]`, `output_mode: "content" | "files_with_matches" | "count"`, `glob: string`, `type: string`, `-i: boolean`, `-A: number`, `-B: number`, `-C: number`, `-n: boolean`, `head_limit: number`, `multiline: boolean` |
| `web_fetch` | `url: string` | `max_length: number`, `start_index: number`, `raw: boolean` |

The other five listed tools are `skill`, `task`, `read_agent`, `list_agents`,
and `write_agent`. They require their own authority/lifecycle contracts and
are not implicitly covered by basic filesystem/shell policy. No `web_search`
appeared in either listing; do not invent a native schema for it.

Important semantics from the observed descriptions:

- `create` requires an absent file; `edit` requires an existing file. The edit
  JSON Schema surprisingly requires only `path`; a conservative adapter should
  demand explicit replacement strings rather than infer missing content.
  Empty replacement text can represent deletion and must not be mistaken for
  absent input.
- `view.path` can identify a file or directory. `view_range` describes a pair
  of one-based line numbers, with `-1` permitted as the ending line. The bare
  schema does not enforce pair length or those bounds; adapter validation should.
- `glob.paths` and `grep.paths` omit to the session working directory. A list
  contains independent directories, not one concatenated path. Every target
  must be authorized, or multiple targets must be refused until supported.
- Shell calls normally start fresh processes. Reusing `shellId` retains the
  directory associated with that shell's creation; simply applying the latest
  session cwd to an old shell ID is not sufficient authority.
- `mode="sync"` does not guarantee absence of background work: after
  `initial_wait`, a still-running command continues in the background. Explicit
  async and detached operation need stronger ownership policy. `detach=True`
  asks for a process independent of normal session shutdown.
- The list's bash description says default `initial_wait` is 10 seconds, while
  the raw built-in descriptor says 30 seconds. Use an explicit bounded value
  and authoritative task inventories rather than relying on either default.
- `web_fetch` describes default `max_length=5000`, maximum 20000, default
  `start_index=0`, and default `raw=False`; the JSON Schema itself supplies
  types but no numeric bounds. Network policy still needs the real URL/redirect
  boundary, not only output-size validation.

The listed basic schemas omit `additionalProperties: false`. That does not
oblige OtoDock to accept unreviewed fields: a strict adapter can reject unknown
arguments so a later runtime feature does not acquire authority accidentally.

## Descriptor/API compatibility limit

`session.rpc.tools.get_builtin_descriptors(ToolsGetBuiltinDescriptorsRequest())`
failed with `AssertionError` in the pinned generated decoder. A read-only
diagnostic through the same session's underlying JSON-RPC transport returned
33 descriptors. The sole decoding failure is `run_factory`: its input schema
has a top-level `oneOf`, but the generated `BuiltinToolInputSchema` expects a
top-level `type`. Do not assume this typed method is a reliable bootstrap check
with the current SDK/runtime pair.

The raw descriptor set additionally includes `apply_patch`,
`str_replace_editor`, `ask_user`, `fetch_copilot_cli_documentation`,
`factories_manage`, `context_board`, `create_pull_request`, `exit_plan_mode`,
`read_inbox`, `reply_to_comment`, `run_factory`, `manage_schedule`, `send_inbox`,
`sql`, `task_complete`, `generic_tool_search`, `tool_search_tool`, and
`update_todo`. `apply_patch` uses a custom input format and has no JSON input
schema. A model/default change may select a different editor/tool surface;
unrecognized names must remain denied rather than inheriting a nearby mapping.

No-inference inspection surfaces exposed by the pinned generated contract are
`client.rpc.tools.list(...)`,
`session.rpc.tools.initialize_and_validate()`, and
`session.rpc.tools.get_current_metadata()`. The latter two still need checking
against the exact selected session before claiming offered-tool verification.
`session.rpc.tools.execute(ToolsExecuteRequest(arguments, name,
tool_call_id=None))` invokes the native pipeline and is suitable for a separate
deterministic execution probe; listing schemas alone does not establish hook
coverage or denial effectiveness.

## Hook shape and failure behavior

Configure both create and resume with
`hooks={"on_pre_tool_use": handler}`. The Python handler receives a mapping
with `sessionId`, `toolName`, `toolArgs`, `workingDirectory`, and `timestamp`;
its second argument contains `session_id`. The SDK converts wire `cwd` into
`workingDirectory` and numeric epoch-millisecond timestamps into a timezone-aware
UTC `datetime`. The public typed hook input contains neither native tool-call ID
nor tool-source classification. Bind to a trusted SDK/platform session and
working directory instead of taking the payload as permission to switch them.

Output fields are `permissionDecision` (`allow`, `deny`, `ask`),
`permissionDecisionReason`, `modifiedArgs`, `additionalContext`, and
`suppressOutput`. A one-shot policy floor should return explicit `allow` only
after the entire operation is authorized, otherwise explicit `deny`; it should
not silently rewrite arguments or downgrade unavailable authority to `ask`.
The release documentation describes `deny` as blocking execution and `None`
as allowing unchanged execution. This distinction matters because the SDK
`_handle_hooks_invoke` catches ordinary exceptions, logs a traceback, and returns
`None`. Raising a normal exception from the host policy is therefore **not a
fail-closed denial**. Catch and sanitize expected failures inside the policy
handler, including controlled cancellation, and produce explicit denial.
[Pinned hook documentation](https://github.com/github/copilot-sdk/blob/v1.0.13/docs/hooks/pre-tool-use.md).

SDK source pre-registers local create/resume session handlers before issuing
the native create/resume RPC. This avoids a normal startup gap when the caller
actually supplies the hook. Hooks are local callback registrations; retaining
history or resuming an ID does not reinstall a missing Python callback.
`enable_file_hooks=False` disables discovered filesystem hooks, not explicitly
registered SDK callbacks.
[Pinned session implementation](https://github.com/github/copilot-sdk/blob/v1.0.13/python/copilot/session.py),
[pinned client implementation](https://github.com/github/copilot-sdk/blob/v1.0.13/python/copilot/client.py).

## Initial policy and bootstrap recommendations

Use `mode="empty"` and a source-qualified exact allowlist such as
`ToolSet().add_builtin(["view", "create", "edit", "bash"])`, expanded only when
each mapping is implemented and tested. **Source qualification alone does not
prevent an explicit SDK built-in override.** The real runtime rejected an
ordinary custom `Tool(name="view")` collision, but accepted
`Tool(name="view", overrides_built_in_tool=True, skip_permission=True)` even
with `available_tools=["builtin:view"]`; that custom handler executed under the
native-looking hook name. A gate mapping names alone cannot distinguish it.
Avoid wildcard tool sets. `BUILTIN_TOOLS_ISOLATED` is not the initial policy
allowlist: it includes agent, inbox, skill, and plan tools outside this adapter's
implemented contract. The hook does not itself expose source information.
This slice therefore requires an empty custom-tool registry and empty MCP
configuration. Future mixed-tool registration needs explicit trusted admission
that rejects built-in overrides/collisions before any SDK registration call.

Canonical mappings should preserve the complete operation: `bash` to `Bash`
with complete command and pinned cwd; `view` to `Read`; `create` to `Write`
with exact path/content; `edit` to `Edit` with exact path/old/new strings;
`glob`/`grep` to the corresponding search authority for every target;
`web_fetch` to `WebFetch` with the exact URL. Explicitly reject unsupported
shell attachment/detachment, async modes, reused shell identities, or multi-path
search rather than pretending those fields cannot affect scope. Normal OtoDock
path resolution and SSRF checks remain mandatory.

Keep config discovery, file hooks, host git operations, and experimental modes
explicitly disabled for the initial session. Enable persistent session storage
only with the existing private state allocation. Install the permission bridge,
owned request registry, and native policy hook on every create/resume; hook
waiting and cancellation must invalidate supervisor settlement snapshots just
like ordinary approval waits.

The pinned permission RPCs expose manual/assisted/allow-all modes, explicit
approve-all flags, rules, path/URL managers, session/location remembered approvals,
and permanent domains. A permission callback alone can be bypassed by those
settings. A pre-tool floor should continue to deny a forbidden operation even
when native read approval or an existing approval is permissive. Bootstrap and
resume must explicitly establish and inspect the chosen policy, while preserving
enterprise denial rules. `reset_session_approvals` alone is not proof that every
other approval source was cleared. See [permission contract](copilot-permission-contract.md)
for exact request/decision fields.

For a deterministic managed-denial test, the pinned SDK additionally accepts
`managed_settings=ManagedSettings(permissions=ManagedSettingsPermissions(deny=["Read(**)"]))`
on create/resume. These classes are defined in `copilot.client`; the source
documents `Read(**)` and `Shell(git push *)` as accepted rule vocabulary and
says injected settings use the native parser also used for fetched policy.
An existing-file `view` with an otherwise allowing hook can therefore test
host-injected managed-denial precedence. That fixture does not establish real
organization-policy retrieval, MDM acquisition, or authenticated enterprise
composition. Keep those claims separate from generic session deny rules too.

Before claiming coverage, the native execution probe must demonstrate blocked
effects for denied shell/read/write/edit calls, a permitted control operation,
denial despite permissive native approvals, sanitized policy failure and held
policy cancellation, and reinstatement on resume. It must inspect actual file,
process, or returned-data effects; merely observing a hook invocation is not
proof of enforcement. This metadata audit does not assert those results.

## Guarded session construction and recorded evidence

The implemented `CopilotNativeToolPolicy.create_session` and `resume_session`
are the protected entry points. Both refuse unrecognized/protected caller
options before any SDK access, enforce `tools=[]`, `mcp_servers={}`, exact
`builtin:` filters and the policy/permission/question callbacks, and disable
config discovery, file hooks, and host git operations. They bind the expected
native session identity before startup activity and validate the actual
model-specific `tools.list` structural schemas before opening the session.
Only these seven extra options are accepted: `model`, `streaming`, `on_event`,
`enable_session_store`, `session_limits`, `system_message`, and
`managed_settings`. This is a host-owned API; options must not be merged over
its mandatory configuration afterward.

A policy/bridge is single-use for one native session opening. Resume needs a
fresh policy/bridge that rebinds the same intended native session and reinstalls
the gate; it never silently creates replacement history. The caller still owns
the pinned sandbox runtime and must close it on failure or cancellation. The
guarded methods do not own process cleanup or independently make an arbitrary
client safe.

There is an additional **history provenance requirement** on resume. In SDK
1.0.13 `client.resume_session`, serialization uses `if tool_defs` before adding
`tools` and `if mcp_servers` before adding `mcpServers`. Passing `tools=[]` and
`mcp_servers={}` therefore omits both fields from the wire; it does **not**
instruct the runtime to erase persisted custom tools or MCP configuration.
The guarded helper may only resume owned history created under the same guarded
native-only profile. It cannot safely convert arbitrary earlier custom-tool,
MCP, or interactive-terminal history merely by supplying empty Python values.
The owning session factory still needs persisted, verified profile provenance
before admitting a resume. Fixed source filters do not replace this requirement.

Guarded resume explicitly uses `continue_pending_work=False` so startup does not
request automatic continuation of previously pending work. This is a native
resume control, not proof that arbitrary old state is safe. Catalog inspection
also has an outer timeout in addition to its RPC timeout; failures still require
the runtime owner to clean up.

The [deterministic native policy report](evidence/copilot-native-policy.json)
records the explicit-override case as an intentionally unsafe control, alongside
guarded create/resume rejection before SDK access and a guarded positive native
view. It also records native denial/effect checks, actual catalog validation,
managed-denial precedence, and owned runtime cleanup. Its no-inference sessions
did not create resumable history: the attempted cold resume failed explicitly
and `same_session_resumed` is false. The report proves guarded resume override
rejection, not successful continuation of a persisted conversation. This
demonstrates why the construction guard is part of the boundary; a source filter
or hook alone must not be advertised as equivalent.

The separate model-driven probe exercises two actual turns: allowed native
creation, followed by cold resume and denied native editing, with native
approve-all enabled in both runs. The [guarded model-driven report](evidence/copilot-native-model.json)
records a passing run in 27.879 seconds, one authority allow and one denial,
one `DONE` per turn, no remaining host request waits, and normal cleanup.
`cold_resume_with_reinstalled_gate=true` proves continuation of this controlled
native-only history with the gate reinstalled; it does not prove sanitization
of a different prior profile. This result is separate from the deterministic
RPC probe's missing-history case.

The explicit no-continuation resume option and outer catalog deadline were added
after that recorded run; their final guarded model regression is pending until
the evidence report is refreshed. The earlier pass must not be presented as
validation of those subsequent changes.
