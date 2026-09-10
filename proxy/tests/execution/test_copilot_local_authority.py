"""Factory owner validity through real mode, path and human-question authority."""

import asyncio
from dataclasses import dataclass
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
def local_authority(monkeypatch):
    sdk, rpc = ModuleType("copilot"), ModuleType("copilot.rpc")
    rpc.PermissionDecisionApproveOnce, rpc.PermissionDecisionReject = ApproveOnce, Reject
    sdk.rpc = rpc
    monkeypatch.setitem(sys.modules, "copilot", sdk)
    monkeypatch.setitem(sys.modules, "copilot.rpc", rpc)
    sid = str(uuid.uuid4())
    monkeypatch.setitem(session_state._sessions, sid, {"client_type": "dashboard"})
    monkeypatch.setitem(session_state._session_modes, sid, "default")
    monkeypatch.setitem(session_state._session_security, sid, SecurityContext(
        role="viewer", username="alice", agent="demo", is_admin_agent=False,
    ))
    owner = SimpleNamespace(value=True, error=False)

    def owner_valid():
        if owner.error:
            raise RuntimeError("private owner failure")
        return owner.value

    requests = CopilotRequestRegistry(lambda: None)
    bridge = bind_platform_authority(
        sid, requests, working_directory="/workspace", expected_sdk_session_id="native-fixture",
        question_timeout=2, owner_valid=owner_valid,
    )
    yield SimpleNamespace(sid=sid, bridge=bridge, requests=requests, owner=owner)
    session_state.resolve_session_permissions(sid, approved=False)
    session_state._permission_emitters.pop(sid, None)
    session_state._session_tool_allows.pop(sid, None)


async def read(authority, path="/workspace/fixture.txt"):
    return await authority.bridge.on_permission_request(
        {"kind": "read", "path": path}, {"session_id": "native-fixture"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False, 0, 1, "true"])
async def test_supplied_owner_requires_exact_true_before_allowing_read(local_authority, value):
    local_authority.owner.value = value
    assert isinstance(await read(local_authority), Reject)
    assert session_state.get_permission_queue(local_authority.sid).empty()
    assert not local_authority.requests.pending_ids


@pytest.mark.asyncio
async def test_owner_exception_is_sanitized_denial(local_authority):
    local_authority.owner.error = True
    result = await read(local_authority)
    assert isinstance(result, Reject)
    assert "private owner failure" not in result.feedback
    assert not local_authority.requests.pending_ids


@pytest.mark.asyncio
async def test_true_owner_still_requires_registered_context(local_authority):
    session_state._session_security.pop(local_authority.sid)
    assert isinstance(await read(local_authority), Reject)


@pytest.mark.asyncio
async def test_true_owner_preserves_real_path_and_plan_floors(local_authority):
    session_state._session_modes[local_authority.sid] = "plan"
    assert isinstance(await read(local_authority), ApproveOnce)
    assert isinstance(await read(local_authority, "/etc/shadow"), Reject)
    result = await local_authority.bridge.on_permission_request(
        {"kind": "write", "file_name": "/workspace/fixture.txt", "diff": "+fixture",
         "new_file_contents": "fixture"}, {"session_id": "native-fixture"},
    )
    assert isinstance(result, Reject)
    assert session_state.get_permission_queue(local_authority.sid).empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unchanged", "missing", "false", "throw"])
async def test_owner_rechecked_after_real_human_approval(local_authority, change):
    session_state._session_security[local_authority.sid] = SecurityContext(
        role="admin", username="", agent="demo", is_admin_agent=True,
    )
    pending = asyncio.create_task(local_authority.bridge.on_permission_request(
        {"kind": "shell", "full_command_text": "frobnicate --needs-confirmation"},
        {"session_id": "native-fixture"},
    ))
    try:
        prompt = await asyncio.wait_for(session_state.get_permission_queue(local_authority.sid).get(), 2)
        assert prompt["event_type"] == "permission_prompt"
        if change == "missing":
            local_authority.owner.value = None
        elif change == "false":
            local_authority.owner.value = False
        elif change == "throw":
            local_authority.owner.error = True
        assert session_state.resolve_permission(prompt["request_id"], True)
        result = await asyncio.wait_for(pending, 2)
        assert isinstance(result, ApproveOnce if change == "unchanged" else Reject)
        assert not local_authority.requests.pending_ids
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_question_answer_requires_owner_to_survive_human_wait(local_authority, expired):
    pending = asyncio.create_task(local_authority.bridge.on_user_input_request(
        {"question": "Choose a color", "choices": ["blue", "green"], "allowFreeform": False},
        {"session_id": "native-fixture"},
    ))
    try:
        prompt = await asyncio.wait_for(session_state.get_permission_queue(local_authority.sid).get(), 2)
        assert prompt["event_type"] == "question_prompt"
        question = prompt["tool_input"]["questions"][0]
        local_authority.owner.value = not expired
        assert session_state.resolve_question(prompt["request_id"], {question["id"]: {"answers": ["blue"]}})
        if expired:
            with pytest.raises(UserInputUnavailableError, match="Copilot user input is unavailable"):
                await asyncio.wait_for(pending, 2)
        else:
            assert await asyncio.wait_for(pending, 2) == {"answer": "blue", "wasFreeform": False}
        assert not local_authority.requests.pending_ids
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
