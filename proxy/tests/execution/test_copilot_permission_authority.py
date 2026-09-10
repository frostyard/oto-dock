"""Copilot mapping through real OtoDock mode/path/prompt authority.

Only the SDK's final decision containers are faked. Session state is local;
policy, command classification, path/URL floors and human queues are real.
"""

import asyncio
from dataclasses import dataclass, replace
import sys
from types import ModuleType, SimpleNamespace
import uuid

import pytest

from auth.path_policy import SecurityContext
from core.layers.copilot.permissions import bind_platform_authority, UserInputUnavailableError
from core.layers.copilot.requests import CopilotRequestRegistry
from core.session import session_state


@dataclass
class ApproveOnce:
    pass


@dataclass
class Reject:
    feedback: str = ""


@pytest.fixture
def authority(monkeypatch):
    sdk = ModuleType("copilot")
    rpc = ModuleType("copilot.rpc")
    rpc.PermissionDecisionApproveOnce = ApproveOnce
    rpc.PermissionDecisionReject = Reject
    sdk.rpc = rpc
    monkeypatch.setitem(sys.modules, "copilot", sdk)
    monkeypatch.setitem(sys.modules, "copilot.rpc", rpc)
    sid = str(uuid.uuid4())
    monkeypatch.setitem(session_state._sessions, sid, {"client_type": "dashboard"})
    monkeypatch.setitem(session_state._session_modes, sid, "default")
    monkeypatch.setitem(session_state._session_security, sid, SecurityContext(
        role="admin", username="", agent="demo", is_admin_agent=True,
    ))
    requests = CopilotRequestRegistry(lambda: None)
    bridge = bind_platform_authority(
        sid, requests, working_directory="/workspace", expected_sdk_session_id="native-fixture",
        question_timeout=1,
    )
    yield SimpleNamespace(sid=sid, requests=requests, bridge=bridge)
    session_state.resolve_session_permissions(sid, approved=False)
    session_state._permission_emitters.pop(sid, None)
    session_state._session_tool_allows.pop(sid, None)


async def decide(authority, request):
    return await authority.bridge.on_permission_request(request, {"session_id": "native-fixture"})


def shell(command="frobnicate --needs-confirmation"):
    return {"kind": "shell", "full_command_text": command}


def write(path="/workspace/fixture.txt"):
    return {"kind": "write", "file_name": path, "diff": "+fixture", "new_file_contents": "fixture"}


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_default_mode_uses_real_human_prompt_and_exact_decision(authority, approve):
    pending = asyncio.create_task(decide(authority, shell()))
    queue = session_state.get_permission_queue(authority.sid)
    prompt = await asyncio.wait_for(queue.get(), 2)
    assert prompt["event_type"] == "permission_prompt"
    assert prompt["tool_name"] == "Bash"
    assert prompt["tool_input"] == {"command": "frobnicate --needs-confirmation", "cwd": "/workspace"}
    assert authority.requests.pending_ids
    assert session_state.resolve_permission(prompt["request_id"], approve)
    result = await asyncio.wait_for(pending, 2)
    assert isinstance(result, ApproveOnce if approve else Reject)
    assert not authority.requests.pending_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["acceptEdits", "dontAsk", "auto"])
async def test_edit_modes_allow_real_path_checked_writes_without_prompt(authority, mode):
    session_state._session_modes[authority.sid] = mode
    result = await asyncio.wait_for(decide(authority, write()), 1)
    assert isinstance(result, ApproveOnce)
    assert session_state.get_permission_queue(authority.sid).empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["dontAsk", "auto"])
async def test_unrecognized_shell_mode_shortcut_still_uses_shared_policy(authority, mode):
    session_state._session_modes[authority.sid] = mode
    assert isinstance(await asyncio.wait_for(decide(authority, shell()), 1), ApproveOnce)
    assert session_state.get_permission_queue(authority.sid).empty()


@pytest.mark.asyncio
async def test_plan_mode_allows_read_but_denies_application_write(authority):
    session_state._session_modes[authority.sid] = "plan"
    assert isinstance(await decide(authority, {"kind": "read", "path": "/workspace/fixture.txt"}), ApproveOnce)
    assert isinstance(await decide(authority, write()), Reject)
    assert session_state.get_permission_queue(authority.sid).empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "dontAsk", "auto"])
async def test_external_shell_floor_cannot_be_lifted_by_any_mode(authority, mode):
    session_state._session_modes[authority.sid] = mode
    session_state._session_security[authority.sid] = SecurityContext(
        role="manager", username="", agent="support", is_admin_agent=False,
        session_scope="agent", principal="external", external_claim="phone:+1",
    )
    assert isinstance(await decide(authority, shell("echo safe-looking")), Reject)
    assert session_state.get_permission_queue(authority.sid).empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["dontAsk", "auto"])
@pytest.mark.parametrize("permission", [
    {"kind": "url", "url": "http://192.168.1.10/admin"},
    {"kind": "read", "path": "/etc/shadow"},
    write("/etc/shadow"),
])
async def test_private_url_and_file_floors_precede_permissive_modes(authority, mode, permission):
    session_state._session_modes[authority.sid] = mode
    session_state._session_security[authority.sid] = SecurityContext(
        role="viewer", username="alice", agent="demo", is_admin_agent=False,
    )
    assert isinstance(await asyncio.wait_for(decide(authority, permission), 1), Reject)
    assert session_state.get_permission_queue(authority.sid).empty()


@pytest.mark.asyncio
async def test_missing_context_rejects_before_prompt_even_in_auto(authority):
    session_state._session_modes[authority.sid] = "auto"
    session_state._session_security.pop(authority.sid)
    assert isinstance(await decide(authority, shell()), Reject)
    assert session_state.get_permission_queue(authority.sid).empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete", "mode", "security", "client", "equal_replacement", "in_place"])
async def test_live_context_change_during_prompt_rejects_late_human_allow(authority, change):
    pending = asyncio.create_task(decide(authority, shell()))
    queue = session_state.get_permission_queue(authority.sid)
    prompt = await asyncio.wait_for(queue.get(), 2)
    if change == "delete":
        session_state._session_security.pop(authority.sid)
    elif change == "mode":
        session_state._session_modes[authority.sid] = "plan"
    elif change == "client":
        session_state._sessions[authority.sid] = {"client_type": "task"}
    elif change == "equal_replacement":
        session_state._session_security[authority.sid] = replace(session_state._session_security[authority.sid])
    elif change == "in_place":
        object.__setattr__(session_state._session_security[authority.sid], "role", "viewer")
    else:
        session_state._session_security[authority.sid] = replace(
            session_state._session_security[authority.sid], role="viewer", is_admin_agent=False,
        )
    assert session_state.resolve_permission(prompt["request_id"], True)
    assert isinstance(await asyncio.wait_for(pending, 2), Reject)


@pytest.mark.asyncio
@pytest.mark.parametrize("client", ["task", "phone", "meeting", "trigger", "internal"])
async def test_unattended_question_never_queues_or_fabricates_answer(authority, client):
    session_state._sessions[authority.sid] = {"client_type": client}
    with pytest.raises(UserInputUnavailableError):
        await asyncio.wait_for(authority.bridge.on_user_input_request(
            {"question": "Choose a color", "choices": ["blue", "green"], "allowFreeform": False},
            {"session_id": "native-fixture"},
        ), 1)
    assert session_state.get_permission_queue(authority.sid).empty()
    assert not authority.requests.pending_ids


@pytest.mark.asyncio
async def test_wrong_sdk_session_cannot_reach_human_authority(authority):
    result = await authority.bridge.on_permission_request(shell(), {"session_id": "other-native-session"})
    assert isinstance(result, Reject)
    assert session_state.get_permission_queue(authority.sid).empty()
