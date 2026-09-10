"""Pinned SDK session RPC adapter, imported without requiring an SDK install."""

from __future__ import annotations

from core.layers.copilot.coordinator import TaskObservation, TaskState
from core.layers.copilot.supervisor import RuntimeSnapshot


_INVALID_SNAPSHOT = "Invalid Copilot runtime inventory"


def _identity(value) -> bool:
    return isinstance(value, str) and bool(value) and value.strip() == value and value.isprintable()


def _status(value):
    return getattr(value, "value", value)


def task_observations(tasks) -> tuple[TaskObservation, ...]:
    """Validate every native tracked task, including shells and client tasks.

    Unknown explicit statuses block settlement. Missing or malformed fields and
    duplicate identities are invalid inventories, never evidence of no work.
    An empty native inventory does not prove absence of untracked processes.
    """
    if not isinstance(tasks, list):
        raise ValueError(_INVALID_SNAPSHOT)
    observations = []
    seen = set()
    for task in tasks:
        task_id = getattr(task, "id", None)
        status = _status(getattr(task, "status", None))
        if not _identity(task_id) or task_id in seen or not _identity(status):
            raise ValueError(_INVALID_SNAPSHOT)
        seen.add(task_id)
        try:
            # Retirement is host process-fence evidence, never a native status.
            state = TaskState.UNKNOWN if status == "retired" else TaskState(status)
        except ValueError:
            state = TaskState.UNKNOWN
        observations.append(TaskObservation(task_id, state))
    return tuple(observations)


def _pending_permissions(result) -> frozenset[str]:
    items = getattr(result, "items", None)
    if not isinstance(items, list):
        raise ValueError(_INVALID_SNAPSHOT)
    identities = set()
    for item in items:
        identity = getattr(item, "request_id", None)
        if not _identity(identity) or identity in identities:
            raise ValueError(_INVALID_SNAPSHOT)
        identities.add(identity)
    return frozenset(identities)


def _pending_messages(result) -> frozenset[str]:
    items = getattr(result, "items", None)
    steering = getattr(result, "steering_messages", None)
    in_flight = getattr(result, "in_flight_steering_count", None)
    if (not isinstance(items, list) or not isinstance(steering, list)
            or any(not isinstance(text, str) for text in steering)
            or (in_flight is not None and (type(in_flight) is not int or not 0 <= in_flight <= len(steering)))):
        raise ValueError(_INVALID_SNAPSHOT)
    for item in items:
        # Batch rows can share a canonical queue ID. Do not deduplicate them or
        # mistake already-delivered steering text for a drained native queue.
        if (not _identity(getattr(item, "id", None))
                or not _identity(_status(getattr(item, "kind", None)))
                or not isinstance(getattr(item, "display_text", None), str)):
            raise ValueError(_INVALID_SNAPSHOT)
    # Display text can contain prompts; retain only occupancy, never contents.
    return frozenset({"native-input"}) if items or steering else frozenset()


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
        if type(getattr(result, "interrupted", None)) is not bool:
            raise ValueError(_INVALID_SNAPSHOT)
        return result.interrupted

    async def is_processing(self) -> bool:
        result = await self.session.rpc.metadata.is_processing(timeout=self.rpc_timeout)
        if type(getattr(result, "processing", None)) is not bool:
            raise ValueError(_INVALID_SNAPSHOT)
        return result.processing

    def _observe_tasks(self, tasks) -> tuple[TaskObservation, ...]:
        return task_observations(tasks)

    async def snapshot(self) -> RuntimeSnapshot:
        tasks = await self.session.rpc.tasks.list(timeout=self.rpc_timeout)
        permissions = await self.session.rpc.permissions.pending_requests(timeout=self.rpc_timeout)
        queue = await self.session.rpc.queue.pending_items(timeout=self.rpc_timeout)
        processing = await self.is_processing()
        return RuntimeSnapshot(
            processing, self._observe_tasks(getattr(tasks, "tasks", None)),
            _pending_permissions(permissions), _pending_messages(queue),
        )

    async def disconnect(self) -> None:
        await self.session.disconnect()
