"""Route process-backed shell stop evidence through real supervisor fences."""

import asyncio
from dataclasses import replace

import pytest

from test_supervisor import make
from test_native_shells import Session, owned, shell, changed
from core.layers.copilot.supervisor import SessionSupervisorError


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["abort", "interrupt"])
async def test_missing_bash_completion_is_cancelled_only_after_owned_control_proof(control):
    supervisor, backend, _ = make()

    async def send(_):
        backend.emit("tool.execution_start", {"toolCallId": "shell-call", "toolName": "bash"})

    backend.send_hook = send
    original = getattr(backend, control)

    async def stop():
        result = await original()
        backend.state = replace(backend.state, native_shells_stopped=True)
        return result

    setattr(backend, control, stop)
    stream = supervisor.stream("run a shell")
    first = await anext(stream)
    while first.type != "tool_use":
        first = await anext(stream)
    ack = await getattr(supervisor, control)()
    assert ack.accepted
    output = [event async for event in stream]
    assert sum(event.type == "done" for event in output) == 1
    results = [event for event in output if event.type == "tool_result"]
    assert len(results) == 1 and results[0].data["is_error"] is True
    assert "owned-process settlement" in results[0].data["result_content"]
    await supervisor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,proof", [("bash", False), ("create", True), ("Bash", True)])
async def test_shell_flag_cannot_close_other_tools_or_replace_missing_process_proof(tool, proof):
    supervisor, backend, closed = make(turn_timeout=0.15)

    async def send(_):
        backend.emit("tool.execution_start", {"toolCallId": "call", "toolName": tool})

    backend.send_hook = send
    original = backend.abort

    async def stop():
        await original()
        backend.state = replace(backend.state, native_shells_stopped=proof)

    backend.abort = stop
    stream = supervisor.stream("fixture")
    await anext(stream)
    await supervisor.abort()
    with pytest.raises(SessionSupervisorError):
        async for event in stream:
            assert event.type != "done"
    assert closed


@pytest.mark.asyncio
async def test_adapter_stop_flag_requires_control_terminal_tasks_and_current_process_proof(monkeypatch):
    # Types are constructed by the SDK only in production; this fixture supplies
    # a control result without credentials or an installed SDK.
    from types import ModuleType, SimpleNamespace
    import sys

    rpc = ModuleType("copilot.rpc")
    rpc.TasksCancelRequest = SimpleNamespace
    monkeypatch.setitem(sys.modules, "copilot.rpc", rpc)
    sdk = Session([shell()])
    proof = [True]
    adapter = owned(sdk, processes_settled=lambda: proof[0])
    assert (await adapter.snapshot()).native_shells_stopped is False

    async def cancel(*_, **__):
        sdk.tasks = [changed(sdk.tasks[0], status="cancelled")]
        return SimpleNamespace(cancelled=True)

    sdk.rpc.tasks.cancel.side_effect = cancel
    await adapter.abort()
    assert (await adapter.snapshot()).native_shells_stopped is True
    proof[0] = False
    assert (await adapter.snapshot()).native_shells_stopped is False
    proof[0] = True
    await adapter.send("next turn")
    assert (await adapter.snapshot()).native_shells_stopped is False


@pytest.mark.asyncio
async def test_invalid_native_shell_stop_flag_poisoned_without_completion():
    supervisor, backend, _ = make()
    backend.state = replace(backend.state, native_shells_stopped=1)
    with pytest.raises(SessionSupervisorError):
        async for event in supervisor.stream("fixture"):
            assert event.type != "done"
    await asyncio.sleep(0)
