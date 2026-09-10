"""The fixed host callback is an owned authority boundary, not native approval."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.callbacks import CallbackRegistry  # noqa: E402
from core.layers.copilot.host_tools import (  # noqa: E402
    CopilotDelegationTool, DELEGATE_TOOL, DELEGATE_CANONICAL, valid_delegate_args, valid_delegation_targets,
)
from core.layers.copilot.native_tool_policy import CopilotNativeToolPolicy, _SCHEMAS  # noqa: E402
from core.layers.copilot.permissions import CopilotPermissionBridge  # noqa: E402
from core.layers.copilot.requests import CopilotRequestRegistry  # noqa: E402


@dataclass
class Result:
    text_result_for_llm: str = ""
    result_type: str = "success"
    error: str | None = None


@pytest.fixture
def harness(monkeypatch):
    tools = ModuleType("copilot.tools")
    tools.ToolResult = Result
    tools.Tool = SimpleNamespace
    rpc = ModuleType("copilot.rpc")
    rpc.PermissionDecisionApproveOnce = lambda: SimpleNamespace(kind="allow")
    rpc.PermissionDecisionReject = lambda **kwargs: SimpleNamespace(kind="deny", **kwargs)
    monkeypatch.setitem(sys.modules, "copilot", ModuleType("copilot"))
    monkeypatch.setitem(sys.modules, "copilot.tools", tools)
    monkeypatch.setitem(sys.modules, "copilot.rpc", rpc)
    h = SimpleNamespace(live=True, calls=[], decisions=[], authorize_count=0, handler=None, decision="allow")

    async def authorize():
        h.authorize_count += 1
        if not h.live:
            raise ValueError("secret")

    async def decide(name, args):
        h.decisions.append((name, deepcopy(args)))
        return {"decision": h.decision}

    async def handle(call_id, args):
        h.calls.append((call_id, deepcopy(args)))
        return await h.handler(call_id, args) if h.handler else "Fixture child completed"

    h.callbacks = CallbackRegistry(lambda: None)
    h.bridge = CopilotPermissionBridge(CopilotRequestRegistry(lambda: None), decide=decide,
                                       context_valid=lambda: h.live, working_directory="/workspace",
                                       expected_sdk_session_id="native-session")
    h.tool = CopilotDelegationTool(targets=("repo", "qa"), handler=handle, bridge=h.bridge,
                                  callbacks=h.callbacks, authorize=authorize)
    h.policy = CopilotNativeToolPolicy(h.bridge, enabled_tools=frozenset({"view"}), delegation=h.tool)
    return h


def args(**changes):
    return {"agent": "repo", "name": "Review", "prompt": "Review this change", **changes}


def invocation(**changes):
    return SimpleNamespace(session_id="native-session", tool_call_id="call-one", tool_name=DELEGATE_TOOL,
                           arguments=args(), **changes)


@pytest.mark.asyncio
async def test_native_admission_cannot_dispatch_and_actual_handler_authorizes_once(harness):
    h = harness
    hook = {"sessionId": "native-session", "workingDirectory": "/workspace", "toolName": DELEGATE_TOOL,
            "toolArgs": args()}
    assert await h.policy.on_pre_tool_use(hook, {"session_id": "native-session"}) == {"permissionDecision": "allow"}
    requested = {"kind": "custom-tool", "tool_name": DELEGATE_TOOL, "args": args()}
    assert (await h.policy.on_permission_request(requested, {"session_id": "native-session"})).kind == "allow"
    assert h.calls == h.decisions == []
    result = await h.tool.execute(invocation())
    assert result.result_type == "success"
    assert h.decisions == [(DELEGATE_CANONICAL, args())]
    assert h.calls == [("call-one", args())] and h.authorize_count == 3
    assert h.callbacks.pending_ids == frozenset()
    assert (await h.tool.execute(invocation())).result_type == "failure"
    assert len(h.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("agent", "other"), ("prompt", ""), ("name", " spaced "), ("name", "x" * 101),
                                          ("prompt", "🙂" * 4097), ("extra", "spoof")])
async def test_schema_or_target_rejection_never_reaches_authority_or_handler(harness, field, value):
    call = invocation()
    call.arguments[field] = value
    assert (await harness.tool.execute(call)).result_type == "failure"
    assert harness.calls == harness.decisions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("session_id", "other"), ("tool_call_id", ""), ("tool_call_id", " spaced "), ("tool_name", "view")])
async def test_exact_native_invocation_binding(harness, field, value):
    call = invocation()
    setattr(call, field, value)
    assert (await harness.tool.execute(call)).result_type == "failure"
    assert harness.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["managed_approval_required", "request_sandbox_bypass", "skip_permission"])
async def test_native_permission_bypass_flags_fail_closed(harness, flag):
    request = {"kind": "custom-tool", "tool_name": DELEGATE_TOOL, "args": args(), flag: True}
    assert (await harness.policy.on_permission_request(request, {"session_id": "native-session"})).kind == "deny"
    assert harness.calls == harness.decisions == []


@pytest.mark.asyncio
async def test_denial_revocation_and_error_outputs_are_sanitized(harness):
    harness.decision = "deny"
    assert (await harness.tool.execute(invocation())).result_type == "failure"
    assert harness.calls == []
    harness.decision = "allow"
    harness.live = False
    assert (await harness.tool.execute(invocation())).result_type == "failure"
    harness.live = True

    async def fail(*_):
        raise RuntimeError("gho_secret_private_error")

    harness.handler = fail
    call = invocation()
    call.tool_call_id = "fresh"
    result = await harness.tool.execute(call)
    assert result.result_type == "failure" and "gho_" not in repr(result)


@pytest.mark.asyncio
async def test_sdk_cancellation_does_not_erase_resistant_host_callback(harness):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def held(*_):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return "completed"

    harness.handler = held
    task = asyncio.create_task(harness.tool.execute(invocation()))
    await entered.wait()
    task.cancel()
    assert (await task).result_type == "failure"
    assert harness.callbacks.pending_ids == {"call-one"}
    harness.callbacks.close_admissions()
    await harness.callbacks.cancel_all(.01)
    assert cancelled.is_set() and harness.callbacks.pending_ids == {"call-one"}
    release.set()
    await harness.callbacks.cancel_all(.1)
    assert not harness.callbacks.pending_ids


def test_trusted_binding_cannot_override_native_or_mcp_session_options(harness):
    options = harness.policy.session_options()
    assert options["available_tools"] == ["builtin:view", "custom:oto_delegate"]
    assert options["mcp_servers"] == {}
    tool = options["tools"][0]
    assert tool.name == DELEGATE_TOOL and tool.skip_permission is False and tool.overrides_built_in_tool is False
    for key in ("tools", "mcp_servers", "hooks", "available_tools"):
        with pytest.raises(ValueError):
            harness.policy.session_options(**{key: []})
    catalog = {"tools": [{"name": "view", "parameters": _SCHEMAS["view"]},
                          {"name": DELEGATE_TOOL, "parameters": {}}]}
    with pytest.raises(ValueError, match="collides"):
        harness.policy.validate_catalog(catalog)


def test_targets_are_bounded_immutable_unique_slugs():
    for bad in (["repo"], ("repo", "repo"), ("../repo",), ("a/b",), ("x" * 257,), tuple(str(i) for i in range(65))):
        assert not valid_delegation_targets(bad)
    assert valid_delegation_targets(()) and valid_delegation_targets(("repo", "qa"))
    assert valid_delegate_args(args(prompt="x" * 16384), ("repo",))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["arguments", "session", "revocation", "cancel"])
async def test_held_approval_cannot_dispatch_mutated_or_revoked_operation(harness, change):
    entered, release = asyncio.Event(), asyncio.Event()
    original_decide = harness.bridge._decide

    async def held(name, values):
        entered.set()
        await release.wait()
        return await original_decide(name, values)

    harness.bridge._decide = held
    call = invocation()
    task = asyncio.create_task(harness.tool.execute(call))
    await entered.wait()
    if change == "arguments":
        call.arguments["prompt"] = "different operation"
    elif change == "session":
        call.session_id = "other"
    elif change == "revocation":
        harness.live = False
    else:
        harness.callbacks.close_admissions()
        await harness.callbacks.cancel_all(.1)
    release.set()
    assert (await task).result_type == "failure"
    assert harness.calls == [] and not harness.callbacks.pending_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["", "x" * 65537, "\0private", 1])
async def test_host_result_bounds_are_enforced(harness, result):
    async def respond(*_):
        return result

    harness.handler = respond
    answer = await harness.tool.execute(invocation())
    assert answer.result_type == "failure" and answer.text_result_for_llm == "Delegation did not complete successfully."
