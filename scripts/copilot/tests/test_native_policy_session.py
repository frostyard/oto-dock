"""Guarded native session admission; fake transport, real policy, no SDK/network."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.native_tool_policy import (  # noqa: E402
    CopilotNativeToolPolicy, NativePolicySessionError,
)
from core.layers.copilot.permissions import CopilotPermissionBridge  # noqa: E402
from core.layers.copilot.requests import CopilotRequestRegistry  # noqa: E402


@pytest.fixture(autouse=True)
def fake_rpc_types(monkeypatch):
    rpc = ModuleType("copilot.rpc")
    rpc.ToolsListRequest = SimpleNamespace
    monkeypatch.setitem(sys.modules, "copilot.rpc", rpc)


def catalog():
    schemas = json.loads((Path(__file__).resolve().parents[3]
                          / "proxy/core/layers/copilot/native_tool_schemas.json").read_text())
    return {"tools": [{"name": name, "parameters": schema} for name, schema in schemas.items()]}


def policy():
    async def authorize(_name, _args):
        return {"decision": "allow"}

    bridge = CopilotPermissionBridge(
        CopilotRequestRegistry(lambda: None), decide=authorize, context_valid=lambda: True,
        working_directory="/workspace",
    )
    return CopilotNativeToolPolicy(bridge, enabled_tools=frozenset({"view"}))


class Client:
    def __init__(self, gate, *, actual_catalog=None, catalog_error=None, open_error=None, returned_id=None):
        self.gate = gate
        self.catalog = catalog() if actual_catalog is None else actual_catalog
        self.catalog_error = catalog_error
        self.open_error = open_error
        self.returned_id = returned_id
        self.calls = []
        self.options = None
        self.catalog_entered = asyncio.Event()
        self.catalog_release = None
        self.rpc = SimpleNamespace(tools=SimpleNamespace(list=self.list_tools))

    async def list_tools(self, request, *, timeout):
        self.calls.append(("catalog", request.model, timeout))
        self.catalog_entered.set()
        if self.catalog_release is not None:
            await self.catalog_release.wait()
        if self.catalog_error:
            raise self.catalog_error
        return SimpleNamespace(to_dict=lambda: deepcopy(self.catalog))

    async def create_session(self, *, session_id, **options):
        return await self.open("create", session_id, options)

    async def resume_session(self, session_id, **options):
        return await self.open("resume", session_id, options)

    async def open(self, kind, session_id, options):
        self.calls.append((kind, session_id))
        self.options = options
        # A transport may immediately invoke a hook before returning the session.
        assert self.gate.bridge.matches_sdk_session(session_id)
        result = await options["hooks"]["on_pre_tool_use"]({
            "sessionId": session_id, "workingDirectory": "/workspace", "toolName": "view",
            "toolArgs": {"path": "/workspace/fixture.txt"},
        }, {"session_id": session_id})
        assert result == {"permissionDecision": "allow"}
        if self.open_error:
            raise self.open_error
        return SimpleNamespace(session_id=self.returned_id or session_id)


async def open_session(gate, client, mode, **options):
    if mode == "create":
        return await gate.create_session(client, session_id="native-fixture", **options)
    return await gate.resume_session(client, "native-fixture", **options)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "resume"])
@pytest.mark.parametrize("key", [
    "tools", "hooks", "mcp_servers", "available_tools", "excluded_tools", "custom_agents",
    "agent", "on_permission_request", "on_user_input_request", "enable_config_discovery",
    "enable_file_hooks", "enable_host_git_operations", "working_directory", "config_dir",
    "unknown_option", "continue_pending_work",
])
async def test_protected_or_unknown_options_are_rejected_before_any_rpc(mode, key):
    gate = policy()
    client = Client(gate)
    with pytest.raises(ValueError, match="cannot be overridden"):
        await open_session(gate, client, mode, **{key: []})
    assert client.calls == []
    assert not gate.bridge.matches_sdk_session("native-fixture")
    # Admission rejection does not consume the policy's one actual open attempt.
    await open_session(gate, client, mode)
    assert [call[0] for call in client.calls] == ["catalog", mode]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "resume"])
async def test_selected_catalog_then_bound_hook_with_mandatory_options(mode):
    gate = policy()
    client = Client(gate)
    callback = lambda _event: None
    safe = {
        "model": "fixture-model", "streaming": True, "on_event": callback,
        "enable_session_store": False, "session_limits": {"max_turns": 2},
        "system_message": {"mode": "append", "content": "Fixture"}, "managed_settings": {},
    }
    session = await open_session(gate, client, mode, **safe)
    assert session.session_id == "native-fixture"
    assert client.calls == [("catalog", "fixture-model", 5), (mode, "native-fixture")]
    assert all(client.options[key] == value for key, value in safe.items())
    if mode == "resume":
        assert client.options["continue_pending_work"] is False
    else:
        assert "continue_pending_work" not in client.options
    assert client.options["available_tools"] == ["builtin:view"]
    assert client.options["tools"] == [] and client.options["mcp_servers"] == {}
    assert client.options["enable_config_discovery"] is False
    assert client.options["enable_file_hooks"] is False
    assert client.options["enable_host_git_operations"] is False
    assert client.options["on_permission_request"] == gate.bridge.on_permission_request
    assert client.options["on_user_input_request"] == gate.bridge.on_user_input_request
    with pytest.raises(NativePolicySessionError):
        await open_session(gate, client, mode)
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_generated_identity_is_bound_before_catalog_and_startup():
    gate = policy()
    client = Client(gate)
    client.catalog_release = asyncio.Event()
    opening = asyncio.create_task(gate.create_session(client))
    await client.catalog_entered.wait()
    # Reuse is denied while the first open is still in flight.
    with pytest.raises(NativePolicySessionError):
        await gate.create_session(client)
    client.catalog_release.set()
    session = await opening
    assert isinstance(session.session_id, str) and len(session.session_id) == 32
    assert gate.bridge.matches_sdk_session(session.session_id)
    assert len(client.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "resume"])
async def test_explicit_identity_is_bound_before_catalog_rpc(mode):
    gate = policy()
    client = Client(gate)
    client.catalog_release = asyncio.Event()
    opening = asyncio.create_task(open_session(gate, client, mode))
    await client.catalog_entered.wait()
    assert gate.bridge.matches_sdk_session("native-fixture")
    client.catalog_release.set()
    await opening


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "resume"])
@pytest.mark.parametrize("change", ["missing", "duplicate", "schema", "malformed"])
async def test_catalog_drift_stops_before_session_rpc_and_consumes_attempt(mode, change):
    actual = catalog()
    selected = next(tool for tool in actual["tools"] if tool["name"] == "view")
    if change == "missing":
        actual["tools"].remove(selected)
    elif change == "duplicate":
        actual["tools"].append(deepcopy(selected))
    elif change == "schema":
        selected["parameters"]["properties"]["path"]["type"] = "number"
    else:
        actual = {"tools": "secret-invalid-catalog"}
    gate = policy()
    client = Client(gate, actual_catalog=actual)
    with pytest.raises(NativePolicySessionError) as error:
        await open_session(gate, client, mode)
    assert error.value.__context__ is None
    assert [call[0] for call in client.calls] == ["catalog"]
    with pytest.raises(NativePolicySessionError):
        await open_session(gate, client, mode)
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "resume"])
@pytest.mark.parametrize("stage", ["catalog", "open"])
async def test_sdk_errors_are_sanitized_and_failed_resume_never_creates(mode, stage):
    gate = policy()
    client = Client(gate, **{f"{stage}_error": RuntimeError("secret-transport-token")})
    with pytest.raises(NativePolicySessionError) as error:
        await open_session(gate, client, mode)
    assert "secret" not in str(error.value)
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert [call[0] for call in client.calls] == (["catalog"] if stage == "catalog" else ["catalog", mode])
    with pytest.raises(NativePolicySessionError):
        await open_session(gate, client, mode)
    assert len(client.calls) == (1 if stage == "catalog" else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "resume"])
async def test_returned_identity_mismatch_fails_closed(mode):
    gate = policy()
    client = Client(gate, returned_id="wrong-session")
    with pytest.raises(NativePolicySessionError) as error:
        await open_session(gate, client, mode)
    assert error.value.__context__ is None
    assert gate.bridge.matches_sdk_session("native-fixture")
    assert not gate.bridge.matches_sdk_session("wrong-session")
    assert [call[0] for call in client.calls] == ["catalog", mode]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "resume"])
async def test_cancellation_propagates_without_retry_or_fallback(mode):
    gate = policy()
    client = Client(gate)
    client.catalog_release = asyncio.Event()
    opening = asyncio.create_task(open_session(gate, client, mode))
    await client.catalog_entered.wait()
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    with pytest.raises(NativePolicySessionError):
        await open_session(gate, client, mode)
    assert [call[0] for call in client.calls] == ["catalog"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "resume"])
@pytest.mark.parametrize("native_id", ["", "\n", 123, True])
async def test_invalid_identity_never_reaches_sdk(mode, native_id):
    gate = policy()
    client = Client(gate)
    with pytest.raises(NativePolicySessionError):
        if mode == "create":
            await gate.create_session(client, session_id=native_id)
        else:
            await gate.resume_session(client, native_id)
    assert client.calls == []


@pytest.mark.asyncio
async def test_explicit_builtin_override_tool_is_rejected_before_transport():
    gate = policy()
    client = Client(gate)
    handler_calls = []

    async def handler(*args):
        handler_calls.append(args)
        return "unapproved"

    override = SimpleNamespace(name="view", overrides_built_in_tool=True, handler=handler)
    with pytest.raises(ValueError, match="cannot be overridden"):
        await gate.create_session(client, tools=[override])
    assert client.calls == [] and handler_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "resume"])
async def test_conflicting_preexisting_bridge_binding_never_reaches_transport(mode):
    gate = policy()
    gate.bridge.bind_sdk_session("different-session")
    client = Client(gate)
    with pytest.raises(NativePolicySessionError) as error:
        await open_session(gate, client, mode)
    assert error.value.__context__ is None
    assert client.calls == []
    assert gate.bridge.matches_sdk_session("different-session")
    with pytest.raises(NativePolicySessionError):
        await gate.resume_session(client, "different-session")
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["create", "resume"])
async def test_startup_cancellation_propagates_and_does_not_allow_reopening(mode):
    gate = policy()
    client = Client(gate, open_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await open_session(gate, client, mode)
    with pytest.raises(NativePolicySessionError):
        await open_session(gate, client, mode)
    assert [call[0] for call in client.calls] == ["catalog", mode]
