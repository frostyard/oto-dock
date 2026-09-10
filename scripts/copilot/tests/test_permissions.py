"""Pure request mapping and owned SDK permission/question response tests."""

import asyncio
from dataclasses import dataclass
import functools
from pathlib import Path
import sys
import traceback
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.permissions import CopilotPermissionBridge, UserInputUnavailableError, map_permission
from core.layers.copilot.requests import CopilotRequestRegistry


def async_test(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return wrapper


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    sdk = ModuleType("copilot")
    rpc = ModuleType("copilot.rpc")

    @dataclass
    class Approve:
        approved_interactively: bool | None = None
        kind = "approve-once"

    @dataclass
    class Reject:
        feedback: str | None = None
        kind = "reject"

    rpc.PermissionDecisionApproveOnce = Approve
    rpc.PermissionDecisionReject = Reject
    monkeypatch.setitem(sys.modules, "copilot", sdk)
    monkeypatch.setitem(sys.modules, "copilot.rpc", rpc)


def bridge(*, decide=None, ask=None, context_valid=lambda: True, **kwargs):
    async def allow(_name, _args):
        return {"decision": "allow"}
    registry = CopilotRequestRegistry(lambda: None)
    return CopilotPermissionBridge(registry, decide=decide or allow, ask=ask,
                                   context_valid=context_valid, working_directory="/workspace", **kwargs)


@pytest.mark.parametrize("payload,expected", [
    ({"kind": "shell", "full_command_text": "rm /workspace/file", "read_only": True},
     ("Bash", {"command": "rm /workspace/file", "cwd": "/workspace"})),
    ({"kind": "read", "path": "/workspace/file"}, ("Read", {"file_path": "/workspace/file"})),
    ({"kind": "write", "file_name": "/workspace/file", "diff": "change", "new_file_contents": "new"},
     ("Write", {"file_path": "/workspace/file", "diff": "change", "content": "new"})),
    ({"kind": "url", "url": "http://127.0.0.1/"}, ("WebFetch", {"url": "http://127.0.0.1/"})),
    ({"kind": "mcp", "server_name": "file-tools-mcp", "tool_name": "save", "args": {"path": "/x"}},
     ("mcp__file-tools-mcp__save", {"path": "/x"})),
])
def test_complete_operations_reach_shared_authority_without_trusting_readonly_hints(payload, expected):
    assert map_permission(payload, working_directory="/workspace") == expected
    assert map_permission(SimpleNamespace(**payload), working_directory="/workspace") == expected


@pytest.mark.parametrize("payload", [
    {"kind": "memory"}, {"kind": "hook", "tool_name": "Read"}, {"kind": "factory"},
    {"kind": "extension-management"}, {"kind": "custom-tool", "tool_name": "Read"},
    {"kind": "custom-tool", "tool_name": "Bash"},
    {"kind": "shell", "full_command_text": ""},
    {"kind": "read", "path": "relative"}, {"kind": "read", "path": "/tmp/\x00secret"},
    {"kind": "write", "file_name": "/tmp/file"},
    {"kind": "mcp", "server_name": "a__b", "tool_name": "c"},
    {"kind": "mcp", "server_name": "a", "tool_name": "b", "args": "invalid"},
])
def test_unknown_ambiguous_or_incomplete_requests_are_denied(payload):
    assert map_permission(payload, working_directory="/workspace") is None


@pytest.mark.parametrize("flag", ["managed_approval_required", "request_sandbox_bypass", "skip_permission"])
@pytest.mark.parametrize("value", [True, 0, "false"])
def test_managed_human_bypass_and_malformed_flags_cannot_autoapprove(flag, value):
    assert map_permission({"kind": "read", "path": "/workspace/file", flag: value},
                          working_directory="/workspace") is None


@async_test
async def test_only_explicit_trusted_custom_binding_can_use_custom_authority():
    seen = []

    async def decide(name, args):
        seen.append((name, args))
        return {"decision": "allow"}

    bindings = {"oto_permission_probe": "CopilotCustomTool"}
    handler = bridge(decide=decide, custom_tools=bindings)
    bindings["Read"] = "Read"  # Later caller mutation cannot add privilege.
    result = await handler.on_permission_request({"kind": "custom-tool", "tool_name": "oto_permission_probe", "args": {}}, {})
    assert result.kind == "approve-once" and result.approved_interactively is None
    assert seen == [("CopilotCustomTool", {})]
    assert (await handler.on_permission_request({"kind": "custom-tool", "tool_name": "Read"}, {})).kind == "reject"


@async_test
@pytest.mark.parametrize("decision", [{"decision": "deny"}, {"decision": "ask"}, {"decision": "defer"},
                                      {"decision": True}, {}, None,
                                      {"decision": "allow", "updated_input": {"file_path": "/other"}}])
async def test_only_allow_without_input_rewrite_can_execute(decision):
    async def decide(*_):
        return decision
    result = await bridge(decide=decide).on_permission_request({"kind": "read", "path": "/workspace/file"}, {})
    assert result.kind == "reject"


@async_test
@pytest.mark.parametrize("mutation", ["context", "args", "request"])
async def test_approval_cannot_outlive_context_or_operation_mutation(mutation):
    valid = True
    request = {"kind": "read", "path": "/workspace/file"}

    async def decide(_name, args):
        nonlocal valid
        if mutation == "context":
            valid = False
        elif mutation == "args":
            args["file_path"] = "/other"
        else:
            request["path"] = "/other"
        return {"decision": "allow"}

    result = await bridge(decide=decide, context_valid=lambda: valid).on_permission_request(request, {})
    assert result.kind == "reject"


@async_test
async def test_missing_context_and_wrong_native_session_never_reach_authority():
    async def forbidden(*_):
        pytest.fail("Unbound callback reached authority")
    request = {"kind": "read", "path": "/workspace/file"}
    assert (await bridge(decide=forbidden, context_valid=lambda: False).on_permission_request(request, {})).kind == "reject"
    handler = bridge(decide=forbidden, expected_sdk_session_id="expected")
    assert (await handler.on_permission_request(request, {"session_id": "other"})).kind == "reject"
    with pytest.raises(ValueError):
        handler.bind_sdk_session("other")


@async_test
@pytest.mark.parametrize("ignore_cancel", [False, True])
async def test_cancelled_or_stale_permission_wait_returns_reject(ignore_cancel):
    entered = asyncio.Event()

    async def decide(*_):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not ignore_cancel:
                raise
            return {"decision": "allow"}

    handler = bridge(decide=decide)
    task = asyncio.create_task(handler.on_permission_request({"kind": "read", "path": "/workspace/file"}, {}))
    await entered.wait()
    await handler.requests.cancel_all(1)
    assert (await task).kind == "reject"
    assert not handler.requests.pending_ids


@async_test
@pytest.mark.parametrize("choice,freeform,expected", [("A", False, False), ("Other choice", True, True)])
async def test_correlated_question_returns_exact_answer_and_freeform_flag(choice, freeform, expected):
    async def ask(questions):
        assert questions[0]["isOther"] is freeform and questions[0]["multiSelect"] is False
        return {questions[0]["id"]: {"answers": [choice]}}
    result = await bridge(ask=ask).on_user_input_request({"question": "Choose", "choices": ["A", "B"], "allowFreeform": freeform}, {})
    assert result == {"answer": choice, "wasFreeform": expected}


@async_test
@pytest.mark.parametrize("changed", ["question", "choices", "allowFreeform"])
async def test_question_answer_cannot_outlive_source_mutation(changed):
    request = {"question": "Original question", "choices": ["A", "B"], "allowFreeform": False}
    entered, release = asyncio.Event(), asyncio.Event()

    async def ask(questions):
        entered.set()
        await release.wait()
        # The presented question remains the original, even if the source's
        # choices list is mutated in place while the user considers it.
        assert questions[0]["question"] == "Original question"
        assert [option["label"] for option in questions[0]["options"]] == ["A", "B"]
        assert questions[0]["isOther"] is False
        return {questions[0]["id"]: {"answers": ["A"]}}

    handler = bridge(ask=ask)
    task = asyncio.create_task(handler.on_user_input_request(request, {}))
    await asyncio.wait_for(entered.wait(), 1)
    if changed == "choices":
        request["choices"].append("Injected choice")
    elif changed == "question":
        request[changed] = "Injected question"
    else:
        request[changed] = True
    release.set()
    with pytest.raises(UserInputUnavailableError) as failure:
        await asyncio.wait_for(task, 1)
    assert "Injected" not in str(failure.value)
    assert failure.value.__context__ is None
    assert not handler.requests.pending_ids


@async_test
async def test_chosen_label_plus_dashboard_free_text_are_both_preserved():
    async def ask(questions):
        return {questions[0]["id"]: {"answers": ["A", "Please keep my extra detail"]}}
    result = await bridge(ask=ask).on_user_input_request({
        "question": "Choose", "choices": ["A", "B"], "allowFreeform": True,
    }, {})
    assert result == {"answer": "A\nPlease keep my extra detail", "wasFreeform": True}


@async_test
@pytest.mark.parametrize("answer", [[], ["A", "B"], ["not a choice"], [""], [123]])
async def test_bad_question_answer_cannot_become_user_consent(answer):
    async def ask(questions):
        return {questions[0]["id"]: {"answers": answer}}
    with pytest.raises(UserInputUnavailableError) as exc:
        await bridge(ask=ask).on_user_input_request({"question": "Choose", "choices": ["A", "B"], "allowFreeform": False}, {})
    assert exc.value.__context__ is None


@async_test
async def test_question_unavailable_unattended_and_cancelled_never_fabricate_answer():
    async def unattended(_):
        return {}
    for ask in (None, unattended):
        with pytest.raises(UserInputUnavailableError):
            await bridge(ask=ask).on_user_input_request({"question": "Choose"}, {})
    entered = asyncio.Event()

    async def held(_):
        entered.set()
        await asyncio.Event().wait()

    handler = bridge(ask=held)
    task = asyncio.create_task(handler.on_user_input_request({"question": "Choose"}, {}))
    await entered.wait()
    await handler.requests.cancel_all(1)
    with pytest.raises(UserInputUnavailableError) as exc:
        await task
    assert exc.value.__context__ is None and not handler.requests.pending_ids


@async_test
async def test_raw_policy_and_question_errors_are_not_exposed():
    async def fail(*_):
        raise ValueError("ghu_private_fixture")
    handler = bridge(decide=fail, ask=fail)
    result = await handler.on_permission_request({"kind": "read", "path": "/workspace/file"}, {})
    assert result.kind == "reject" and "ghu_private_fixture" not in result.feedback
    with pytest.raises(UserInputUnavailableError) as exc:
        await handler.on_user_input_request({"question": "Choose"}, {})
    assert "ghu_private_fixture" not in "".join(traceback.format_exception(exc.value))
    assert exc.value.__context__ is None
