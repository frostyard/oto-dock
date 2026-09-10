"""Native built-in input boundaries and shared authorization without inference."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.native_tool_policy import CopilotNativeToolPolicy  # noqa: E402
from core.layers.copilot.permissions import CopilotPermissionBridge  # noqa: E402
from core.layers.copilot.requests import CopilotRequestRegistry  # noqa: E402


TOOLS = frozenset({"bash", "create", "edit", "view", "glob", "grep"})


def native(tool="view", args=None, **changes):
    return {
        "sessionId": "native-session", "workingDirectory": "/workspace", "toolName": tool,
        "toolArgs": {"path": "/workspace/file.txt"} if args is None else args, **changes,
    }


def setup(*, decide=None, bound=True, live=None, enabled=TOOLS):
    calls = []
    live = live if live is not None else [True]

    async def authority(name, args):
        calls.append((name, deepcopy(args)))
        return await decide(name, args) if decide else {"decision": "allow"}

    bridge = CopilotPermissionBridge(
        CopilotRequestRegistry(lambda: None), decide=authority, context_valid=lambda: live[0],
        working_directory="/workspace", expected_sdk_session_id="native-session" if bound else None,
    )
    return CopilotNativeToolPolicy(bridge, enabled_tools=enabled), bridge, calls


async def invoke(gate, payload, invocation=None):
    return await gate.on_pre_tool_use(payload, {"session_id": "native-session"} if invocation is None else invocation)


def assert_denied(result):
    assert isinstance(result, dict) and result.get("permissionDecision") == "deny"
    assert "modifiedArgs" not in result


@pytest.mark.asyncio
async def test_native_view_reaches_shared_authority_with_exact_absolute_path():
    gate, _, calls = setup()
    result = await invoke(gate, native())
    assert result["permissionDecision"] == "allow"
    assert calls == [("Read", {"file_path": "/workspace/file.txt"})]
    assert "modifiedArgs" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    native(sessionId="other"), native(workingDirectory="/different"),
    native(workingDirectory="relative"), native(tool="web_fetch", args={"url": "http://127.0.0.1"}),
    native(tool="unknown_builtin"), native(tool="view", args={"path": "relative.txt"}),
    native(tool="view", args={"path": "/workspace/file", "unexpected": True}),
    native(tool="view", args={"path": "/workspace/file", "skip_permission": True}),
    native(tool="view", args="not an object"),
])
async def test_unknown_or_unbound_native_operation_is_explicitly_denied(payload):
    gate, _, calls = setup()
    assert_denied(await invoke(gate, payload))
    assert calls == []


@pytest.mark.asyncio
async def test_native_hook_requires_both_session_bindings_and_enabled_tool():
    gate, _, calls = setup(bound=False)
    assert_denied(await invoke(gate, native()))
    assert calls == []
    gate, _, calls = setup()
    assert_denied(await invoke(gate, native(), invocation={"session_id": "other"}))
    assert calls == []
    gate, _, calls = setup(enabled=frozenset({"bash"}))
    assert_denied(await invoke(gate, native()))
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"shellId": "existing-shell"}, {"detach": True}, {"mode": "async"},
    {"command": ""}, {"command": "echo ok", "unexpected": 1},
])
async def test_bash_reuse_detach_async_and_unknown_args_are_rejected(changes):
    gate, _, calls = setup()
    args = {"command": "echo ok", "description": "Print a fixture", **changes}
    assert_denied(await invoke(gate, native("bash", args)))
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "authority_input", "context", "rewrite"])
async def test_native_allow_cannot_outlive_operation_or_context_changes(change):
    source = native()
    live = [True]

    async def decide(_name, args):
        if change == "source":
            source["toolArgs"]["path"] = "/different"
        elif change == "authority_input":
            args["file_path"] = "/different"
        elif change == "context":
            live[0] = False
        else:
            return {"decision": "allow", "updated_input": {"file_path": "/different"}}
        return {"decision": "allow"}

    gate, _, _ = setup(decide=decide, live=live)
    assert_denied(await invoke(gate, source))


@pytest.mark.asyncio
async def test_native_policy_errors_are_explicit_denials_without_secret_error_text():
    async def decide(*_):
        raise RuntimeError("private-token-and-path")

    gate, _, _ = setup(decide=decide)
    result = await invoke(gate, native())
    assert_denied(result)
    assert "private-token" not in repr(result)


@pytest.mark.asyncio
async def test_cancelled_native_hook_returns_deny_while_policy_remains_owned():
    started = asyncio.Event()

    async def decide(*_):
        started.set()
        await asyncio.Event().wait()

    gate, bridge, _ = setup(decide=decide)
    waiter = asyncio.create_task(invoke(gate, native()))
    await started.wait()
    waiter.cancel()
    assert_denied(await waiter)
    assert bridge.requests.pending_ids
    await bridge.requests.cancel_all(0.5)
    assert not bridge.requests.pending_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args,expected", [
    ("bash", {"command": "pwd", "description": "Show cwd"},
     ("Bash", {"command": "pwd", "description": "Show cwd", "cwd": "/workspace"})),
    ("create", {"path": "/workspace/new.txt", "file_text": "new"},
     ("Write", {"file_path": "/workspace/new.txt", "content": "new"})),
    ("edit", {"path": "/workspace/existing.txt", "old_str": "old", "new_str": "new"},
     ("Edit", {"file_path": "/workspace/existing.txt", "old_string": "old", "new_string": "new"})),
    ("glob", {"pattern": "**/*.py", "paths": ["/workspace/project"]},
     ("Glob", {"path": "/workspace/project", "pattern": "**/*.py"})),
    ("grep", {"pattern": "needle", "-i": True, "head_limit": 20},
     ("Grep", {"path": "/workspace", "pattern": "needle", "-i": True, "head_limit": 20})),
])
async def test_native_operations_project_complete_reviewed_arguments(tool, args, expected):
    gate, _, calls = setup()
    original = deepcopy(args)
    assert (await invoke(gate, native(tool, args)))["permissionDecision"] == "allow"
    assert calls == [expected] and args == original


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("glob", {"pattern": "../outside/*"}), ("glob", {"pattern": "/outside/*"}),
    ("glob", {"pattern": "folder\\*"}),
    ("glob", {"pattern": "*", "paths": []}),
    ("glob", {"pattern": "*", "paths": ["/workspace", "/outside"]}),
    ("grep", {"pattern": "needle", "paths": "relative"}),
    ("grep", {"pattern": "needle", "paths": ["/workspace", "/outside"]}),
    ("grep", {"pattern": "needle", "glob": "../outside/*"}),
    ("grep", {"pattern": "needle", "glob": "/outside/*"}),
    ("grep", {"pattern": "needle", "head_limit": True}),
    ("grep", {"pattern": "needle", "-C": 1.5}),
    ("grep", {"pattern": "needle", "output_mode": "future-mode"}),
    ("view", {"path": "/workspace/file", "view_range": [0, 5]}),
    ("view", {"path": "/workspace/file", "view_range": [5, 4]}),
    ("view", {"path": "/workspace/file", "view_range": [1, 2, 3]}),
    ("view", {"path": "/workspace/file", "forceReadLargeFiles": 1}),
    ("edit", {"path": "/workspace/file"}),
    ("edit", {"path": "/workspace/file", "old_str": "", "new_str": "new"}),
    ("create", {"path": "/workspace/file", "file_text": None}),
    ("bash", {"command": "pwd"}),
    ("bash", {"command": "pwd", "description": "Show cwd", "initial_wait": float("nan")}),
    ("bash", {"command": "pwd", "description": "Show cwd", "initial_wait": 0}),
])
async def test_incomplete_or_unqualified_native_argument_shapes_deny_before_authority(tool, args):
    gate, _, calls = setup()
    assert_denied(await invoke(gate, native(tool, args)))
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"skip_permission": True}, {"cachedApproval": True}, {"source": "mcp"}])
async def test_native_hook_does_not_trust_cached_or_skip_permission_payload_fields(extra):
    gate, _, calls = setup()
    assert_denied(await invoke(gate, native(**extra)))
    assert calls == []


def catalog():
    schemas = json.loads((Path(__file__).resolve().parents[3] / "proxy/core/layers/copilot/native_tool_schemas.json").read_text())
    return {"tools": [{"name": name, "parameters": schema} for name, schema in schemas.items()]}


def test_catalog_accepts_reviewed_schema_and_ignores_descriptions_only():
    gate, _, _ = setup()
    source = catalog()
    gate.validate_catalog(source)
    source["tools"][0]["description"] = "Changed human-readable text"
    source["tools"][0]["parameters"]["description"] = "Changed human-readable schema text"
    source["tools"].append({"name": "disabled_new_vendor_tool", "parameters": {"type": "object"}})
    gate.validate_catalog(source)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "field_type", "description_field_type",
                                      "required", "new_field", "enum", "invalid"])
def test_catalog_structural_drift_never_silently_enables_native_tools(mutation):
    gate, _, _ = setup()
    source = catalog()
    bash = next(tool for tool in source["tools"] if tool["name"] == "bash")
    if mutation == "missing":
        source["tools"].remove(bash)
    elif mutation == "duplicate":
        source["tools"].append(deepcopy(bash))
    elif mutation == "field_type":
        bash["parameters"]["properties"]["command"]["type"] = "array"
    elif mutation == "description_field_type":
        bash["parameters"]["properties"]["description"]["type"] = "array"
    elif mutation == "required":
        bash["parameters"]["required"] = ["command"]
    elif mutation == "new_field":
        bash["parameters"]["properties"]["skipPermission"] = {"type": "boolean"}
    elif mutation == "enum":
        bash["parameters"]["properties"]["mode"]["enum"].append("unrestricted")
    else:
        source = {"tools": "malformed"}
    with pytest.raises(ValueError):
        gate.validate_catalog(source)


def test_session_options_filter_builtin_source_and_install_every_required_boundary():
    gate, bridge, _ = setup(enabled=frozenset({"view", "glob"}))
    options = gate.session_options()
    assert options["available_tools"] == ["builtin:glob", "builtin:view"]
    assert options["tools"] == [] and options["mcp_servers"] == {}
    assert options["hooks"] == {"on_pre_tool_use": gate.on_pre_tool_use}
    assert options["on_permission_request"] == bridge.on_permission_request
    assert options["on_user_input_request"] == bridge.on_user_input_request
    assert options["enable_config_discovery"] is False
    assert options["enable_file_hooks"] is False
    assert options["enable_host_git_operations"] is False
    options["available_tools"].append("*")
    options["hooks"].clear()
    options["mcp_servers"]["injected"] = {}
    assert gate.session_options()["available_tools"] == ["builtin:glob", "builtin:view"]
    assert gate.session_options()["hooks"] == {"on_pre_tool_use": gate.on_pre_tool_use}
    assert gate.session_options()["mcp_servers"] == {}


@pytest.mark.parametrize("enabled", [set(TOOLS), frozenset(), frozenset({"web_fetch"}), frozenset({"*"})])
def test_enabled_builtin_subset_must_be_explicit_and_supported(enabled):
    with pytest.raises(ValueError):
        setup(enabled=enabled)


@pytest.mark.asyncio
async def test_native_hook_preprocessing_exception_cannot_become_sdk_fail_open():
    class Uncopyable(dict):
        def __deepcopy__(self, _memo):
            raise RuntimeError("private-native-payload")

    gate, _, calls = setup()
    result = await invoke(gate, Uncopyable(native()))
    assert_denied(result)
    assert "private-native-payload" not in repr(result) and calls == []
