"""Native tool gate through real OtoDock path, mode, and human prompt policy."""

import asyncio
from types import SimpleNamespace
import uuid

import pytest

from auth.path_policy import SecurityContext
from core.layers.copilot.native_tool_policy import CopilotNativeToolPolicy
from core.layers.copilot.permissions import bind_platform_authority
from core.layers.copilot.requests import CopilotRequestRegistry
from core.session import session_state


@pytest.fixture
def authority(monkeypatch):
    sid = str(uuid.uuid4())
    monkeypatch.setitem(session_state._sessions, sid, {"client_type": "dashboard"})
    monkeypatch.setitem(session_state._session_modes, sid, "default")
    monkeypatch.setitem(session_state._session_security, sid, SecurityContext(
        role="admin", username="", agent="demo", is_admin_agent=True,
    ))
    registry = CopilotRequestRegistry(lambda: None)
    bridge = bind_platform_authority(sid, registry, working_directory="/workspace",
                                    expected_sdk_session_id="native-fixture")
    gate = CopilotNativeToolPolicy(bridge, enabled_tools=frozenset({"bash", "create", "edit", "view", "glob", "grep"}))
    yield SimpleNamespace(sid=sid, registry=registry, gate=gate)
    session_state.resolve_session_permissions(sid, approved=False)
    session_state._permission_emitters.pop(sid, None)
    session_state._session_tool_allows.pop(sid, None)


async def invoke(authority, tool, args):
    if tool == "bash":
        args = {"description": "Run fixture command", **args}
    return await authority.gate.on_pre_tool_use({
        "sessionId": "native-fixture", "workingDirectory": "/workspace", "toolName": tool, "toolArgs": args,
    }, {"session_id": "native-fixture"})


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_native_default_shell_uses_real_human_decision(authority, approve):
    pending = asyncio.create_task(invoke(authority, "bash", {"command": "frobnicate --needs-confirmation"}))
    prompt = await asyncio.wait_for(session_state.get_permission_queue(authority.sid).get(), 2)
    assert prompt["event_type"] == "permission_prompt" and prompt["tool_name"] == "Bash"
    assert session_state.resolve_permission(prompt["request_id"], approve)
    result = await asyncio.wait_for(pending, 2)
    assert result["permissionDecision"] == ("allow" if approve else "deny")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["acceptEdits", "dontAsk", "auto"])
async def test_native_create_uses_edit_mode_and_real_path_checks(authority, mode):
    session_state._session_modes[authority.sid] = mode
    result = await invoke(authority, "create", {"path": "/workspace/new.txt", "file_text": "new content"})
    assert result["permissionDecision"] == "allow"
    assert session_state.get_permission_queue(authority.sid).empty()


@pytest.mark.asyncio
async def test_native_plan_mode_read_and_write_differ(authority):
    session_state._session_modes[authority.sid] = "plan"
    assert (await invoke(authority, "view", {"path": "/workspace/file.txt"}))["permissionDecision"] == "allow"
    assert (await invoke(authority, "create", {"path": "/workspace/new.txt", "file_text": "new"}))["permissionDecision"] == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "dontAsk", "auto"])
async def test_native_external_shell_is_denied_in_every_mode(authority, mode):
    session_state._session_modes[authority.sid] = mode
    session_state._session_security[authority.sid] = SecurityContext(
        role="manager", username="", agent="support", is_admin_agent=False,
        session_scope="agent", principal="external", external_claim="phone:+1",
    )
    assert (await invoke(authority, "bash", {"command": "echo harmless"}))["permissionDecision"] == "deny"
    assert session_state.get_permission_queue(authority.sid).empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("view", {"path": "/etc/shadow"}),
    ("view", {"path": "/workspace/../etc/shadow"}),
    ("create", {"path": "/etc/shadow", "file_text": "replacement"}),
    ("edit", {"path": "/etc/shadow", "old_str": "old", "new_str": "new"}),
    ("glob", {"paths": ["/etc"], "pattern": "*"}),
    ("grep", {"paths": "/etc", "pattern": "secret"}),
    ("web_fetch", {"url": "http://127.0.0.1/admin"}),
])
async def test_native_private_path_floor_and_unsupported_web_tool_never_prompt(authority, tool, args):
    session_state._session_modes[authority.sid] = "auto"
    session_state._session_security[authority.sid] = SecurityContext(
        role="viewer", username="alice", agent="demo", is_admin_agent=False,
    )
    assert (await invoke(authority, tool, args))["permissionDecision"] == "deny"
    assert session_state.get_permission_queue(authority.sid).empty()


@pytest.mark.asyncio
async def test_native_prompt_approval_cannot_survive_deleted_security_context(authority):
    pending = asyncio.create_task(invoke(authority, "bash", {"command": "frobnicate --needs-confirmation"}))
    prompt = await asyncio.wait_for(session_state.get_permission_queue(authority.sid).get(), 2)
    session_state._session_security.pop(authority.sid)
    assert session_state.resolve_permission(prompt["request_id"], True)
    assert (await asyncio.wait_for(pending, 2))["permissionDecision"] == "deny"
