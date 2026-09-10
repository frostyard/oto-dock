"""Pinned RPC adapter filtering without installing or authenticating the SDK."""

from enum import Enum
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.sdk_session import CopilotSdkSession, task_observations  # noqa: E402
from core.layers.copilot.coordinator import SettlementObservation, TaskState  # noqa: E402


def session(*, status="completed", processing=False, queue=None):
    return NS(rpc=NS(
        tasks=NS(list=AsyncMock(return_value=NS(tasks=[NS(id="task", status=NS(value=status))]))),
        permissions=NS(pending_requests=AsyncMock(return_value=NS(items=[]))),
        queue=NS(pending_items=AsyncMock(return_value=NS(items=[], steering_messages=[]) if queue is None else queue)),
        metadata=NS(is_processing=AsyncMock(return_value=NS(processing=processing))),
    ))


@pytest.mark.asyncio
async def test_retained_completed_tasks_are_terminal_and_unsupported_states_stay_unknown():
    completed = await CopilotSdkSession(session()).snapshot()
    assert completed.tasks[0].state is TaskState.COMPLETED
    assert completed.pending_messages == frozenset()
    unknown = await CopilotSdkSession(session(status="new-provider-state")).snapshot()
    assert unknown.tasks[0].state is TaskState.UNKNOWN


@pytest.mark.asyncio
async def test_queue_observation_retains_occupancy_without_private_prompt_text():
    private = "private prompt must not enter snapshot"
    snapshot = await CopilotSdkSession(session(queue=NS(items=[NS(id="queued", kind="message", display_text=private)], steering_messages=[private]))).snapshot()
    assert snapshot.pending_messages and private not in repr(snapshot)


@pytest.mark.asyncio
async def test_native_snapshot_failure_is_not_converted_to_empty_state():
    sdk = session()
    sdk.rpc.permissions.pending_requests.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await CopilotSdkSession(sdk).snapshot()


@pytest.mark.asyncio
async def test_all_pending_native_permission_ids_are_preserved():
    sdk = session(processing=True)
    sdk.rpc.permissions.pending_requests.return_value = NS(items=[NS(request_id="one"), NS(request_id="two")])
    snapshot = await CopilotSdkSession(sdk).snapshot()
    assert snapshot.pending_permissions == frozenset({"one", "two"})
    assert snapshot.processing is True


def settled(snapshot):
    return SettlementObservation(
        snapshot.processing, snapshot.tasks, snapshot.pending_permissions, frozenset(),
        pending_messages=snapshot.pending_messages,
    ).is_settled()


@pytest.mark.parametrize("tasks", [None, {}, "", (), False, 0, [None], [{}], [NS(id="missing-status")],
    [NS(status="completed")], [NS(id="same", status="completed"), NS(id="same", status="completed")],
    [NS(id="same", status="completed"), NS(id="same", status="running")]])
def test_malformed_or_duplicate_task_inventory_is_rejected(tasks):
    with pytest.raises(ValueError, match="^Invalid Copilot runtime inventory$"):
        task_observations(tasks)


@pytest.mark.parametrize("identity", [None, "", " ", " padded", "trailing ", "line\nbreak", 0, True, [], {}])
def test_task_id_must_be_nonempty_opaque_text(identity):
    with pytest.raises(ValueError, match="^Invalid Copilot runtime inventory$"):
        task_observations([NS(id=identity, status="completed")])


@pytest.mark.parametrize("status", [None, "", " ", " completed", "completed\n", 0, True, [], {}, NS()])
def test_malformed_task_status_is_not_converted_to_unknown(status):
    with pytest.raises(ValueError, match="^Invalid Copilot runtime inventory$") as error:
        task_observations([NS(id="task", status=status)])
    assert error.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "idle", "orphaned", "new-native-state", "retired"])
async def test_shell_and_client_tasks_are_not_filtered_and_nonterminal_blocks(status):
    sdk = session()
    sdk.rpc.tasks.list.return_value = NS(tasks=[
        NS(id="agent", type="agent", status="completed"),
        NS(id="shell", type="shell", status=status, command="private shell command"),
        NS(id="client", type="client", status="completed"),
    ])
    snapshot = await CopilotSdkSession(sdk).snapshot()
    assert tuple(task.task_id for task in snapshot.tasks) == ("agent", "shell", "client")
    assert not settled(snapshot)
    assert "private shell command" not in repr(snapshot)
    if status in {"new-native-state", "retired"}:
        assert snapshot.tasks[1].state is TaskState.UNKNOWN


@pytest.mark.asyncio
async def test_actual_enum_shape_and_all_terminal_states_are_preserved():
    class NativeState(Enum):
        COMPLETED = "completed"
        FAILED = "failed"
        CANCELLED = "cancelled"

    sdk = session()
    sdk.rpc.tasks.list.return_value = NS(tasks=[
        NS(id=state.value, status=state) for state in NativeState
    ])
    snapshot = await CopilotSdkSession(sdk).snapshot()
    assert tuple(task.state for task in snapshot.tasks) == (
        TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED,
    )
    assert settled(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, NS(), NS(tasks=None), NS(tasks={}), NS(tasks="")])
async def test_invalid_task_response_cannot_become_an_empty_snapshot(result):
    sdk = session()
    sdk.rpc.tasks.list.return_value = result
    with pytest.raises(ValueError, match="^Invalid Copilot runtime inventory$"):
        await CopilotSdkSession(sdk).snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("items", [None, {}, "", (), False, [None], [NS()], [NS(request_id="")],
    [NS(request_id=False)], [NS(request_id=" padded")],
    [NS(request_id="same"), NS(request_id="same")]])
async def test_invalid_or_duplicate_permission_inventory_cannot_collapse(items):
    sdk = session()
    sdk.rpc.permissions.pending_requests.return_value = NS(items=items)
    with pytest.raises(ValueError, match="^Invalid Copilot runtime inventory$"):
        await CopilotSdkSession(sdk).snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("queue", [
    NS(), NS(items=None, steering_messages=[]), NS(items={}, steering_messages=[]),
    NS(items="", steering_messages=[]), NS(items=(), steering_messages=[]),
    NS(items=[], steering_messages=None), NS(items=[], steering_messages={}),
    NS(items=[], steering_messages=""), NS(items=[], steering_messages=()),
    NS(items=[], steering_messages=[None]), NS(items=[], steering_messages=[False]),
    NS(items=[None], steering_messages=[]), NS(items=["raw prompt"], steering_messages=[]),
    NS(items=[NS(id="", kind="message", display_text="prompt")], steering_messages=[]),
    NS(items=[NS(id="id", kind=None, display_text="prompt")], steering_messages=[]),
    NS(items=[NS(id="id", kind="message", display_text=None)], steering_messages=[]),
])
async def test_invalid_queue_inventory_is_not_treated_as_empty(queue):
    with pytest.raises(ValueError, match="^Invalid Copilot runtime inventory$"):
        await CopilotSdkSession(session(queue=queue)).snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [-1, 2, True, False, 0.0, "0"])
async def test_in_flight_steering_count_must_be_consistent_integer(count):
    queue = NS(items=[], steering_messages=["private"], in_flight_steering_count=count)
    with pytest.raises(ValueError, match="^Invalid Copilot runtime inventory$"):
        await CopilotSdkSession(session(queue=queue)).snapshot()


@pytest.mark.asyncio
async def test_batched_queue_ids_and_in_flight_steering_still_block_settlement():
    queue = NS(items=[
        NS(id="batch", kind="message", display_text="private first"),
        NS(id="batch", kind="message", display_text="private second"),
    ], steering_messages=["private steering"], in_flight_steering_count=1)
    snapshot = await CopilotSdkSession(session(queue=queue)).snapshot()
    assert snapshot.pending_messages == frozenset({"native-input"})
    assert not settled(snapshot)
    assert "private" not in repr(snapshot)
    queue.items.clear()
    snapshot = await CopilotSdkSession(session(queue=queue)).snapshot()
    assert snapshot.pending_messages == frozenset({"native-input"})
    assert not settled(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
async def test_processing_inventory_requires_an_actual_boolean(value):
    adapter = CopilotSdkSession(session(processing=value))
    with pytest.raises(ValueError, match="^Invalid Copilot runtime inventory$"):
        await adapter.is_processing()
    with pytest.raises(ValueError, match="^Invalid Copilot runtime inventory$"):
        await adapter.snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
async def test_interrupt_ack_requires_an_actual_boolean(monkeypatch, value):
    rpc = ModuleType("copilot.rpc")
    rpc.InterruptMainTurnRequest = NS
    monkeypatch.setitem(sys.modules, "copilot.rpc", rpc)
    sdk = session()
    sdk.rpc.interrupt_main_turn = AsyncMock(return_value=NS(interrupted=value))
    with pytest.raises(ValueError, match="^Invalid Copilot runtime inventory$"):
        await CopilotSdkSession(sdk).interrupt()


@pytest.mark.asyncio
async def test_task_observation_override_runs_after_complete_rpc_read_without_second_tasks_rpc():
    sdk = session()
    calls = []

    class Adapter(CopilotSdkSession):
        def _observe_tasks(self, tasks):
            assert self.session.rpc.metadata.is_processing.await_count == 1
            calls.append(tasks)
            return super()._observe_tasks(tasks)

    snapshot = await Adapter(sdk).snapshot()
    assert snapshot.tasks[0].state is TaskState.COMPLETED
    assert len(calls) == 1
    sdk.rpc.tasks.list.assert_awaited_once_with(timeout=5)
