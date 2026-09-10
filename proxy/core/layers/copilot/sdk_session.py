"""Pinned SDK session RPC adapter, imported without requiring an SDK install."""

from __future__ import annotations

from core.layers.copilot.coordinator import TaskObservation, TaskState
from core.layers.copilot.supervisor import RuntimeSnapshot


class CopilotSdkSession:
    """Wrap a session whose tools/hooks/config were explicitly selected by its owner.

    This adapter does not enable native question/permission policy, config
    discovery, tools or plugins. The owner must inventory host pending requests
    separately. Unsupported task states remain unknown and cannot settle.
    """

    def __init__(self, session, *, rpc_timeout: float = 5) -> None:
        self.session = session
        self.rpc_timeout = rpc_timeout

    async def send(self, prompt: str, *, immediate: bool = False) -> str:
        return await self.session.send(prompt, mode="immediate" if immediate else "enqueue")

    async def abort(self) -> None:
        await self.session.abort()

    async def interrupt(self) -> bool:
        from copilot.rpc import InterruptMainTurnRequest

        result = await self.session.rpc.interrupt_main_turn(
            InterruptMainTurnRequest(), timeout=self.rpc_timeout,
        )
        return result.interrupted

    async def is_processing(self) -> bool:
        result = await self.session.rpc.metadata.is_processing(timeout=self.rpc_timeout)
        return result.processing

    async def snapshot(self) -> RuntimeSnapshot:
        tasks = await self.session.rpc.tasks.list(timeout=self.rpc_timeout)
        permissions = await self.session.rpc.permissions.pending_requests(timeout=self.rpc_timeout)
        queue = await self.session.rpc.queue.pending_items(timeout=self.rpc_timeout)
        processing = await self.is_processing()
        observations = []
        for task in tasks.tasks:
            status = getattr(task.status, "value", task.status)
            try:
                state = TaskState(status)
            except (ValueError, TypeError):
                state = TaskState.UNKNOWN
            observations.append(TaskObservation(task.id, state))
        # Queue display text can contain prompts. Only occupancy is needed for
        # settlement; do not copy its contents into control state or diagnostics.
        pending = frozenset({"native-input"}) if queue.items or queue.steering_messages else frozenset()
        return RuntimeSnapshot(
            processing, tuple(observations),
            frozenset(item.request_id for item in permissions.items), pending,
        )

    async def disconnect(self) -> None:
        await self.session.disconnect()
