"""Pinned RPC adapter filtering without installing or authenticating the SDK."""

from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.sdk_session import CopilotSdkSession  # noqa: E402
from core.layers.copilot.coordinator import TaskState  # noqa: E402


def session(*, status="completed", processing=False, queue=None):
    return NS(rpc=NS(
        tasks=NS(list=AsyncMock(return_value=NS(tasks=[NS(id="task", status=NS(value=status))]))),
        permissions=NS(pending_requests=AsyncMock(return_value=NS(items=[]))),
        queue=NS(pending_items=AsyncMock(return_value=queue or NS(items=[], steering_messages=[]))),
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
    snapshot = await CopilotSdkSession(session(queue=NS(items=[private], steering_messages=[private]))).snapshot()
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
